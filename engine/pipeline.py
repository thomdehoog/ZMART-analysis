"""
Pipeline -- how a recipe is read and how its jobs are tracked.

Not part of the public API; the Engine uses it. It reads a recipe (YAML),
splits its steps into phases at scope boundaries, and keeps track of which
jobs are waiting for which scope to complete.

Internal pipeline state and YAML parsing.
-----------------------------------------
Not part of the public API. Used by the Engine to manage registered
pipelines, track jobs, handle scope completion, and accumulate results.

Phases
------
A pipeline's step list is split into phases at scope boundaries:

    Phase 0 (immediate):   preprocess -> segment     [runs per job]
    -- scope: group --
    Phase 1 (scoped):      stitch -> features         [runs once per group]
    -- scope: all --
    Phase 2 (scoped):      normalize                   [runs once for all]

Phase 0 has no scope trigger -- it runs immediately when a job is submitted.
Subsequent phases wait for scope completion signals.

Scope matching
--------------
When complete="X" is signaled from a submit with scope={"X": val}:
  - If "X" is a key in the scope dicts: match by value (scope["X"] == val),
    together with the value of every wider level (see scope_key)
  - If "X" is not a key: collect everything from the previous phase

This means "all" is not special -- it works because no job has "all" as a
scope key, so the engine collects everything.

The same matching holds at every level, not only the first. A compartment
result remembers the scope of the submit that completed it, for example
{"carrier": 1, "compartment": 3}, so a carrier step collects only its own
compartments even while another carrier is still being acquired. A carrier
step also waits for any of its compartments still being analysed, because
the signal that closes a carrier can arrive from another thread before that
compartment's step has finished.


ScopeError
"""

from __future__ import annotations

import logging
import queue
import threading
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


# -- Scope identity ----------------------------------------------------


def _matches(entry_scope, key):
    """Whether a job's scope belongs to the unit *key* names.

    *key* is a dict such as {"compartment": 3, "carrier": 1}: the level being
    completed and every wider level. All of them must agree, because
    compartment 3 exists on every carrier. ``None`` matches everything.
    """
    if key is None:
        return True
    return all(entry_scope.get(k) == v for k, v in key.items())


# -- Data structures ---------------------------------------------------


@dataclass
class StepConfig:
    """Configuration for one step in a pipeline phase.

    Where a step runs is not configured here. The step file names its own
    conda environment in ``METADATA``, beside the imports that need it, so a
    recipe describes only the analysis: the steps, their order, their
    parameters and their scopes. Which environment each step actually used,
    and the package versions in it, is recorded in every result under
    ``provenance``.
    """
    name: str
    params: dict
    #: The pipeline's word on how many of this step may run at once, over
    #: the step file's own METADATA. A step file is written for its heaviest
    #: caller; a pipeline whose work is light (a watershed, not Cellpose)
    #: may run the same file wide without a second copy of it.
    max_workers: int | None = None


@dataclass
class Phase:
    """A group of sequential steps with an optional scope trigger."""
    steps: list
    scope: str | None = None


# -- YAML parsing ------------------------------------------------------


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

    ``scope`` and ``max_workers`` are the engine's keys on a step and are
    taken off before the rest reaches the step as its params. An
    ``environment`` key is refused: the step file owns its environment.
    """
    phases = []
    current_steps = []
    current_scope = None

    for step_dict in steps_config:
        name = list(step_dict.keys())[0]
        raw_params = dict(step_dict[name] or {})
        scope = raw_params.pop("scope", None)
        if "environment" in raw_params:
            raise ValueError(
                f"Step '{name}' sets 'environment' in the pipeline YAML. The "
                "step file owns its environment: set it in the step's "
                "METADATA, or write a separate step file for a different "
                "environment."
            )
        max_workers = raw_params.pop("max_workers", None)

        if scope is not None:
            if current_steps:
                phases.append(Phase(steps=current_steps, scope=current_scope))
            current_steps = []
            current_scope = scope

        current_steps.append(
            StepConfig(
                name=name, params=raw_params,
                max_workers=int(max_workers) if max_workers is not None else None,
            )
        )

    if current_steps:
        phases.append(Phase(steps=current_steps, scope=current_scope))

    return phases


# -- Pipeline state ----------------------------------------------------


class PipelineState:
    """
    Internal state for one registered pipeline.

    Tracks jobs, scope groups, result accumulation, and status counters.
    Thread-safe: all mutable state is protected by _lock.
    """

    def __init__(self, name, yaml_path, phases, functions_dir,
                 step_settings, verbose):
        self.name = name
        self.yaml_path = Path(yaml_path)
        self.phases = phases
        self.functions_dir = functions_dir
        self.step_settings = step_settings
        self.verbose = verbose
        self.workflow_name = name

        self._lock = threading.Lock()
        self._submission_counter = 0

        # Job tracking: [(future, scope_dict, submission_idx)]
        self._job_entries = []

        # Phase 0 results: [(submission_idx, scope_dict, result)]
        self._phase0_results = []

        # Phase N>0 results: phase_idx -> [(submission_idx, scope_dict, result)]
        # where submission_idx is the submit that closed the unit.
        self._phase_results = defaultdict(list)

        # Scoped phases still running: phase_idx -> [(scope_dict, idx, Event)].
        # A later phase waits on the ones of its unit that were signalled
        # before it (idx <= its own); a signal sent after it is not waited
        # on, or the signal threads could all wait on each other.
        self._scoped_in_flight = defaultdict(list)

        # Completed results queue (drained by engine.results())
        self._results_queue = queue.Queue()

        # Status counters. The failed count is always derived from
        # _failures so the two cannot drift apart when scope collection
        # drains consumed failures.
        self._n_submitted = 0
        self._n_pending = 0
        self._n_running = 0
        self._n_completed = 0
        self._failures = []

    def next_submission_idx(self):
        """Get the next submission index (thread-safe)."""
        with self._lock:
            idx = self._submission_counter
            self._submission_counter += 1
            self._n_submitted += 1
            self._n_pending += 1
            return idx

    def add_job_entry(self, future, scope, submission_idx):
        """Record a submitted job's future and scope."""
        with self._lock:
            self._job_entries.append((future, scope, submission_idx))
        future.add_done_callback(
            lambda done: self.record_cancellation(scope, submission_idx)
            if done.cancelled() else None
        )

    def record_start(self, is_submission=True):
        """Move an operation from pending to running."""
        with self._lock:
            if is_submission:
                self._n_pending = max(0, self._n_pending - 1)
            self._n_running += 1

    def store_phase0_result(self, submission_idx, scope, result):
        """Store a Phase 0 result for later scope collection."""
        with self._lock:
            self._phase0_results.append((submission_idx, scope, result))

    def publish_result(self, result, phase_idx, scope, scope_level):
        """Put a completed result in the results queue."""
        result["_phase"] = phase_idx
        result["_scope"] = scope
        result["_scope_level"] = scope_level
        self._results_queue.put(result)

    def record_completion(self):
        """Record that a job/phase completed successfully."""
        with self._lock:
            self._n_running = max(0, self._n_running - 1)
            self._n_completed += 1

    def record_failure(self, scope, step_name, error_msg, phase, submission_idx):
        """Record a failed step. ``phase`` is the phase it failed in and
        ``submission_idx`` the tile, or the submit that closed the unit."""
        with self._lock:
            self._n_running = max(0, self._n_running - 1)
            self._failures.append({
                "scope": scope,
                "step": step_name,
                "error": error_msg,
                "phase": phase,
                "submission_idx": submission_idx,
            })

    def record_cancellation(self, scope, submission_idx):
        """Record a queued submission cancelled during engine shutdown."""
        with self._lock:
            self._n_pending = max(0, self._n_pending - 1)
            self._failures.append({
                "scope": scope,
                "step": "engine",
                "error": "Cancelled during engine shutdown",
                "phase": 0,
                "submission_idx": submission_idx,
            })

    def scope_key(self, level, scope):
        """The identity of the unit *level* closes, from a submit's scope.

        The level's own value together with the value of every wider level
        in this pipeline (the scope levels of later phases) that the scope
        names. Completing compartment 3 of {"carrier": 1, "compartment": 3}
        is compartment 3 *of carrier 1*. ``None`` when the scope does not name the level,
        which collects everything (the "all" case).
        """
        if level not in scope:
            return None
        levels = [phase.scope for phase in self.phases if phase.scope]
        wider = levels[levels.index(level) + 1:] if level in levels else []
        key = {level: scope[level]}
        key.update({w: scope[w] for w in wider if w in scope})
        return key

    def get_triggered_phase_idx(self, level):
        """Find the phase index triggered by a scope level."""
        for i, phase in enumerate(self.phases):
            if phase.scope == level:
                return i
        return None

    def collect_for_scope(self, phase_idx, value, before):
        """
        Collect the previous phase's results and failures for the unit
        *value*, from submits up to and including *before*.

        For Phase 1 (prev=0): collects from Phase 0 results.
        For Phase N (prev=N-1): collects from phase_results[N-1].

        Returns (results, failures) where results is a list sorted by
        submission order and failures is a list of failure records.
        """
        prev_idx = phase_idx - 1

        if prev_idx > 0:
            self._wait_for_scoped(prev_idx, value, before)

        with self._lock:
            if prev_idx == 0:
                entries = self._phase0_results
            else:
                entries = self._phase_results[prev_idx]
            matching, remaining = [], []
            for entry in entries:
                idx, entry_scope, result = entry
                if idx <= before and _matches(entry_scope, value):
                    matching.append((idx, result))
                else:
                    remaining.append(entry)
            if prev_idx == 0:
                self._phase0_results = remaining
            else:
                self._phase_results[prev_idx] = remaining
            matching.sort(key=lambda x: x[0])
            results = [r for _, r in matching]
            failures = self._take_failures(prev_idx, value, before)
            return results, failures

    def _take_failures(self, phase_idx, value, before):
        """Remove and return the failures of phase *phase_idx* that belong
        to the unit *value*, from submits up to *before*. Under _lock."""
        taken, remaining = [], []
        for f in self._failures:
            if (f["phase"] == phase_idx and f["submission_idx"] <= before
                    and _matches(f["scope"], value)):
                taken.append(f)
            else:
                remaining.append(f)
        self._failures = remaining
        return taken

    def begin_scoped(self, phase_idx, scope, submission_idx):
        """Mark a scoped phase as running for *scope*; returns its token.

        A level with no phase in this pipeline gets a token that marks
        nothing, so the caller can treat every level alike.
        """
        done = threading.Event()
        if phase_idx is not None:
            with self._lock:
                self._scoped_in_flight[phase_idx].append(
                    (dict(scope), submission_idx, done))
        return phase_idx, done

    def end_scoped(self, token):
        """Mark a scoped phase as finished, whether it succeeded or not."""
        phase_idx, done = token
        if phase_idx is not None:
            with self._lock:
                self._scoped_in_flight[phase_idx] = [
                    entry for entry in self._scoped_in_flight[phase_idx]
                    if entry[2] is not done
                ]
        done.set()

    def _wait_for_scoped(self, phase_idx, value, before):
        """Wait until every phase *phase_idx* of this unit signalled up to
        *before* is done."""
        with self._lock:
            waiting = [
                done for entry_scope, idx, done
                in self._scoped_in_flight[phase_idx]
                if idx <= before and _matches(entry_scope, value)
            ]
        for done in waiting:
            done.wait()

    def store_phase_result(self, phase_idx, result, scope, submission_idx):
        """Store a scoped phase result, with its scope and the submit that
        closed it, for the next phase."""
        with self._lock:
            self._phase_results[phase_idx].append(
                (submission_idx, dict(scope), result))

    def get_matching_futures(self, value, before):
        """Phase 0 futures of the unit *value*, submitted up to *before*."""
        with self._lock:
            return [
                f for f, scope, idx in self._job_entries
                if idx <= before and _matches(scope, value)
            ]

    def cleanup_consumed_entries(self, value, before):
        """Remove consumed job entries after scope collection."""
        with self._lock:
            self._job_entries = [
                (f, s, idx) for f, s, idx in self._job_entries
                if not (idx <= before and _matches(s, value))
            ]

    def drain_results(self):
        """Drain and return all completed results."""
        results = []
        while True:
            try:
                results.append(self._results_queue.get_nowait())
            except queue.Empty:
                break
        return results

    @property
    def status(self):
        """Current pipeline state for observability."""
        with self._lock:
            return {
                "pending": self._n_pending,
                "running": self._n_running,
                "completed": self._n_completed,
                "failed": len(self._failures),
                "failures": list(self._failures),
            }


class ScopeError(Exception):
    """Invalid scope configuration, missing results, or bad completion signal."""
