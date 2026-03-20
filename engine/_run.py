"""
Run — Pipeline run with phased execution and scope tracking.

A Run represents one analysis pipeline configuration that processes multiple
jobs through phases separated by scope boundaries.

Phases
------
A pipeline's step list is split into phases at scope boundaries:

    Phase 0 (immediate):   preprocess → segment     [runs per job]
    ── scope: spatial ──
    Phase 1 (scoped):      stitch → features         [runs once per spatial group]
    ── scope: temporal ──
    Phase 2 (scoped):      normalize                  [runs once per temporal group]

Phase 0 has no scope trigger — it runs immediately when a job is submitted.
Subsequent phases wait for scope_complete() to fire.

Data flow at scope boundaries
-----------------------------
Phase 0 produces one pipeline_data per job. When scope_complete() is called,
the engine:
  1. Waits for all Phase 0 jobs in the scope group to finish
  2. Collects their results into a list (submission order preserved)
  3. Passes {"results": [...], "metadata": {...}} to Phase 1's first step

Phase 1 produces one result per scope group. Phase 2 collects all Phase 1
results when its scope completes. Scope only narrows — once data is
aggregated, it stays aggregated.

Lifecycle
---------
    run = engine.create_run("pipeline.yaml", priority=1)
    run.submit("tile_1", data, spatial={"region": "R3"}, temporal={"t": "0"})
    run.submit("tile_2", data, spatial={"region": "R3"}, temporal={"t": "0"})
    future = run.scope_complete(spatial={"region": "R3"}, temporal={"t": "0"})
    result = future.result()

Thread safety
-------------
- _lock protects _job_entries, _pending_results, _phase_results, _all_futures
- Phase execution runs in the engine's ThreadPoolExecutor
- Futures synchronize job completion before scope collection
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from ._errors import ScopeError

if TYPE_CHECKING:
    from ._pipeline import PipelineEngine

logger = logging.getLogger(__name__)


# ── Data structures ──────────────────────────────────────────────


@dataclass
class StepConfig:
    """Configuration for one step in a pipeline phase."""
    name: str
    params: dict


@dataclass
class Phase:
    """A group of sequential steps with an optional scope trigger."""
    steps: list
    scope: dict | None = None


@dataclass
class _Job:
    """Internal: a submitted job with its scope labels."""
    label: str
    input_data: dict
    spatial: dict = field(default_factory=dict)
    temporal: dict = field(default_factory=dict)


# ── Helpers ──────────────────────────────────────────────────────


def _make_scope_key(spatial, temporal):
    """Create a hashable key from scope label dicts."""
    items = []
    for k, v in sorted(spatial.items()):
        items.append(("spatial", k, v))
    for k, v in sorted(temporal.items()):
        items.append(("temporal", k, v))
    return tuple(items)


def _scope_triggers(phase_scope, spatial, temporal):
    """Check if scope_complete args match a phase's trigger scope.

    A phase with scope {"spatial": "region"} triggers when the
    scope_complete call includes a spatial dict with key "region".
    """
    for axis, name in phase_scope.items():
        if axis == "spatial" and name not in (spatial or {}):
            return False
        if axis == "temporal" and name not in (temporal or {}):
            return False
    return True


def parse_yaml(yaml_path):
    """
    Parse a pipeline YAML file.

    Returns
    -------
    tuple of (workflow_name, steps_config, metadata)
    """
    yaml_path = Path(yaml_path)
    with open(yaml_path) as f:
        config = yaml.safe_load(f)

    metadata = config.get("metadata", {})

    workflow_name = None
    for key in config:
        if key != "metadata":
            workflow_name = key
            break

    if not workflow_name:
        raise ValueError(
            "No workflow found in YAML (need a key other than 'metadata')"
        )

    steps = config[workflow_name] or []
    if not steps:
        raise ValueError(f"Workflow '{workflow_name}' has no steps")

    return workflow_name, steps, metadata


def split_phases(steps_config):
    """
    Split a flat step list into phases at scope boundaries.

    A new phase starts when a step declares a scope. Steps before the
    first scope are Phase 0 (immediate). Each subsequent scope starts
    a new phase.
    """
    phases = []
    current_steps = []
    current_scope = None

    for step_dict in steps_config:
        name = list(step_dict.keys())[0]
        raw_params = dict(step_dict[name] or {})
        scope = raw_params.pop("scope", None)

        if scope is not None:
            if current_steps:
                phases.append(Phase(steps=current_steps, scope=current_scope))
            current_steps = []
            current_scope = scope

        current_steps.append(StepConfig(name=name, params=raw_params))

    if current_steps:
        phases.append(Phase(steps=current_steps, scope=current_scope))

    return phases


# ── Run ──────────────────────────────────────────────────────────


class Run:
    """
    A pipeline run with scope tracking and result accumulation.

    Parameters
    ----------
    engine : PipelineEngine
        Parent engine (provides executor and step execution).
    yaml_path : str or Path
        Path to the pipeline YAML file.
    priority : int
        Priority level. Higher = more urgent. 0 = default (FIFO).
    """

    def __init__(self, engine: PipelineEngine, yaml_path, priority=0):
        self._engine = engine
        self._yaml_path = Path(yaml_path)
        self._priority = priority
        self._lock = threading.Lock()

        # Parse YAML
        self._workflow_name, steps_config, self._yaml_meta = parse_yaml(
            yaml_path)
        functions_dir_str = self._yaml_meta.get("functions_dir", "../steps")
        self._functions_dir = (
            self._yaml_path.parent / functions_dir_str
        ).resolve()
        self._isolation = self._yaml_meta.get("isolation", "minimal")
        self._pipeline_env = self._yaml_meta.get("environment")
        self._verbose = self._yaml_meta.get("verbose", 0)
        self._phases = split_phases(steps_config)

        # Job tracking: (future, scope_key, submission_index)
        self._job_entries = []
        self._submission_counter = 0

        # Phase 0 result accumulation: scope_key -> [(index, pipeline_data), ...]
        self._pending_results = defaultdict(list)

        # Phase N>0 result accumulation: phase_idx -> [pipeline_data, ...]
        self._phase_results = defaultdict(list)

        # Status counters (no future references held — avoids memory leak)
        self._n_submitted = 0
        self._n_completed = 0
        self._n_failed = 0

        logger.info("Run created: %s, workflow=%s, %d phases, priority=%d",
                     self._yaml_path.name, self._workflow_name,
                     len(self._phases), self._priority)

    def submit(self, label, input_data=None, spatial=None, temporal=None):
        """
        Submit a job for processing. Non-blocking.

        Phase 0 steps execute immediately in the engine's thread pool.
        If the pipeline has no scopes, all steps run and the future
        resolves with the final result.

        Parameters
        ----------
        label : str
            Human-readable label for this job.
        input_data : dict, optional
            Input data for the pipeline.
        spatial : dict, optional
            Spatial scope labels, e.g. {"region": "R3"}.
        temporal : dict, optional
            Temporal scope labels, e.g. {"timepoint": "t0"}.

        Returns
        -------
        concurrent.futures.Future
            Resolves with the Phase 0 result (or final result if no scopes).
        """
        job = _Job(
            label=label,
            input_data=input_data if input_data is not None else {},
            spatial=spatial or {},
            temporal=temporal or {},
        )
        scope_key = _make_scope_key(job.spatial, job.temporal)

        with self._lock:
            submission_idx = self._submission_counter
            self._submission_counter += 1

        logger.info("Job submitted: %s (scope_key=%s, idx=%d)",
                     label, scope_key, submission_idx)

        future = self._engine._executor.submit(
            self._execute_phase_for_job, 0, job, submission_idx,
        )
        future.add_done_callback(self._on_future_done)
        with self._lock:
            self._job_entries.append((future, scope_key))
            self._n_submitted += 1

        return future

    def scope_complete(self, spatial=None, temporal=None):
        """
        Signal scope completion. Triggers the matching scoped phase.

        Waits for all jobs in the scope group to finish Phase 0, collects
        their results, and executes the triggered phase. Non-blocking —
        returns a future that resolves with the phase result.

        Parameters
        ----------
        spatial : dict, optional
            Spatial scope keys, e.g. {"region": "R3"}.
        temporal : dict, optional
            Temporal scope keys, e.g. {"session": "S1"}.

        Returns
        -------
        concurrent.futures.Future
        """
        spatial = spatial or {}
        temporal = temporal or {}

        logger.info("scope_complete: spatial=%s, temporal=%s",
                     spatial, temporal)

        future = self._engine._executor.submit(
            self._handle_scope_complete, spatial, temporal,
        )
        future.add_done_callback(self._on_future_done)
        with self._lock:
            self._n_submitted += 1
        return future

    def _handle_scope_complete(self, spatial, temporal):
        """Wait for pending jobs, collect results, run triggered phase.

        Matching uses subset semantics: a job matches if its scope labels
        are all covered by the scope_complete arguments. Extra keys in
        scope_complete are tolerated (the job's key must be a subset of
        the complete key, not an exact match).
        """
        # Find which phase is triggered
        triggered = None
        for i, phase in enumerate(self._phases):
            if phase.scope is not None and _scope_triggers(
                    phase.scope, spatial, temporal):
                triggered = i
                break

        if triggered is None:
            raise ScopeError(
                f"No phase matches scope_complete("
                f"spatial={spatial}, temporal={temporal})"
            )

        prev_phase = triggered - 1
        complete_key = set(_make_scope_key(spatial, temporal))

        if prev_phase == 0:
            # Wait for all Phase 0 jobs whose scope labels are covered
            matching_futures = []
            with self._lock:
                for future, key in self._job_entries:
                    if set(key).issubset(complete_key):
                        matching_futures.append(future)

            if not matching_futures:
                raise ScopeError(
                    f"No jobs submitted matching scope_complete("
                    f"spatial={spatial}, temporal={temporal})")

            for f in matching_futures:
                f.result()  # blocks until complete; raises on failure

            # Collect results from all matching scope groups
            with self._lock:
                indexed = []
                consumed_keys = []
                for k, entries in self._pending_results.items():
                    if set(k).issubset(complete_key):
                        indexed.extend(entries)
                        consumed_keys.append(k)
                for k in consumed_keys:
                    del self._pending_results[k]

                # Clean up consumed job entries
                matching_set = set(id(f) for f in matching_futures)
                self._job_entries = [
                    (f, k) for f, k in self._job_entries
                    if id(f) not in matching_set
                ]

            # Sort by submission index to preserve submission order
            indexed.sort(key=lambda item: item[0])
            results = [data for _, data in indexed]
        else:
            # Collect from previous scoped phase: take all results
            with self._lock:
                results = list(self._phase_results[prev_phase])
                self._phase_results[prev_phase].clear()

        if not results:
            raise ScopeError(
                f"No results accumulated for phase {triggered}")

        logger.info("Phase %d triggered with %d results",
                     triggered, len(results))
        return self._execute_phase_with_results(triggered, results)

    def _execute_phase_for_job(self, phase_idx, job, submission_idx=0):
        """Execute Phase 0 steps for one job."""
        phase = self._phases[phase_idx]

        def _engine_log(msg):
            if self._verbose in (1, 3):
                print(msg)

        pipeline_data = {
            "metadata": {
                "label": job.label,
                "datetime": datetime.now().strftime("%Y%m%d-%H%M%S"),
                "workflow_name": self._workflow_name,
                "yaml_filename": self._yaml_path.name,
                "steps": [s.name for s in phase.steps],
                "verbose": self._verbose,
                **{k: v for k, v in self._yaml_meta.items()
                   if k not in ("verbose", "functions_dir", "environment",
                                "isolation")},
            },
            "input": job.input_data,
        }

        _engine_log(f"[engine] Job: {job.label}")

        for step_idx, step in enumerate(phase.steps, start=1):
            _engine_log(
                f"[engine]   Step {step_idx}/{len(phase.steps)}: {step.name}")

            pipeline_data = self._engine._execute_step(
                step, pipeline_data, self._functions_dir,
                self._priority, self._isolation, self._pipeline_env,
            )
            if not isinstance(pipeline_data, dict):
                raise TypeError(
                    f"Step '{step.name}' returned "
                    f"{type(pipeline_data).__name__}, expected dict"
                )

        # Accumulate for next phase if it has a scope trigger
        next_phase = phase_idx + 1
        if next_phase < len(self._phases) and self._phases[next_phase].scope is not None:
            scope_key = _make_scope_key(job.spatial, job.temporal)
            with self._lock:
                self._pending_results[scope_key].append(
                    (submission_idx, pipeline_data))

        return pipeline_data

    def _execute_phase_with_results(self, phase_idx, accumulated_results):
        """Execute a scoped phase with accumulated results."""
        phase = self._phases[phase_idx]

        def _engine_log(msg):
            if self._verbose in (1, 3):
                print(msg)

        pipeline_data = {
            "results": accumulated_results,
            "metadata": {
                "datetime": datetime.now().strftime("%Y%m%d-%H%M%S"),
                "workflow_name": self._workflow_name,
                "yaml_filename": self._yaml_path.name,
                "steps": [s.name for s in phase.steps],
                "phase": phase_idx,
                "n_accumulated": len(accumulated_results),
                "verbose": self._verbose,
            },
        }

        _engine_log(f"[engine] Phase {phase_idx}: "
                     f"{len(accumulated_results)} accumulated results")

        for step_idx, step in enumerate(phase.steps, start=1):
            _engine_log(
                f"[engine]   Step {step_idx}/{len(phase.steps)}: {step.name}")

            pipeline_data = self._engine._execute_step(
                step, pipeline_data, self._functions_dir,
                self._priority, self._isolation, self._pipeline_env,
            )
            if not isinstance(pipeline_data, dict):
                raise TypeError(
                    f"Step '{step.name}' returned "
                    f"{type(pipeline_data).__name__}, expected dict"
                )

        # Store for next phase
        if phase_idx + 1 < len(self._phases):
            with self._lock:
                self._phase_results[phase_idx].append(pipeline_data)

        return pipeline_data

    def _on_future_done(self, future):
        """Callback for completed futures — updates counters without
        holding references to the future object."""
        with self._lock:
            self._n_completed += 1
            if future.exception() is not None:
                self._n_failed += 1

    @property
    def status(self):
        """Current run state for observability."""
        with self._lock:
            pending_scope = {
                str(k): len(v)
                for k, v in self._pending_results.items()
            }
        return {
            "yaml": self._yaml_path.name,
            "workflow": self._workflow_name,
            "priority": self._priority,
            "isolation": self._isolation,
            "phases": len(self._phases),
            "total": self._n_submitted,
            "completed": self._n_completed,
            "failed": self._n_failed,
            "pending_scope_groups": pending_scope,
        }

    def __repr__(self):
        return (f"Run({self._yaml_path.name!r}, "
                f"priority={self._priority}, "
                f"phases={len(self._phases)})")
