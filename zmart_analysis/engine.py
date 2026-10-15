"""
Engine -- what you call to run analyses.

You register a pipeline, submit images to it, and read their output from the
job board. The Engine never runs step code itself: it reads each step's
METADATA without running the file, and hands the work to workers (see
workers.py). Scope handling, phases, the board and per-pipeline state live in
pipeline.py.

A worker lives for as long as a registered pipeline needs it: deregister a
pipeline and the workers no other pipeline needs are stopped.

Thread safety: ``_lock`` guards the registered pipelines and the accepting
flag; each PipelineState has its own lock. ``submit`` and ``status`` may
be called from different threads.
"""

from __future__ import annotations

import ast
import concurrent.futures
import json
import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from .pipeline import PipelineState, parse_yaml, split_phases
from .priority_pool import PriorityThreadPool
from .workers import WorkerPool

logger = logging.getLogger(__name__)


class Engine:
    """
    Runs pipelines: register, submit, status, deregister, shutdown.

    Parameters
    ----------
    max_concurrent : int
        How many jobs run at once (default: 8). Per-image work and scope
        closes each get a thread pool of this size.
    execution_timeout : float or None
        Seconds one step may run before it is killed (default: 300).
    """

    def __init__(self, max_concurrent=8, execution_timeout=300.0):
        self.execution_timeout = execution_timeout
        self._pool = WorkerPool()
        self._executor = PriorityThreadPool(max_workers=max_concurrent)
        # Scope completion waits for phase-0 futures.  Keep that coordination
        # off the priority executor whose futures it awaits, otherwise a
        # completion task can occupy the last executor thread and deadlock the
        # lower-priority work needed to satisfy it.
        self._scope_executor = ThreadPoolExecutor(
            max_workers=max_concurrent,
            thread_name_prefix="engine-scope",
        )
        self._pipelines = {}
        self._registering = set()
        self._accepting = True
        self._lock = threading.Lock()

        # Detect orchestrator's conda environment
        self._default_env = os.environ.get("CONDA_DEFAULT_ENV") or Path(sys.prefix).name

        logger.debug(
            "Engine created: max_concurrent=%d, default_env=%s",
            max_concurrent,
            self._default_env,
        )

    # -- Public API ----------------------------------------------------

    def register(self, name, yaml_path):
        """
        Register a pipeline by name.

        Parses the YAML, resolves functions_dir, reads METADATA from all
        step files via AST, and builds the internal phase structure. A step
        runs in the environment its file names; one that is the
        orchestrator's own becomes None, the orchestrator's interpreter.

        Parameters
        ----------
        name : str
            Short name for the pipeline. Used in all subsequent calls.
        yaml_path : str or Path
            Path to the pipeline YAML file.
        """
        with self._lock:
            if not self._accepting:
                raise RuntimeError("Engine has been shut down")
            if name in self._pipelines or name in self._registering:
                raise ValueError(f"Pipeline '{name}' is already registered")
            self._registering.add(name)

        try:
            yaml_path = Path(yaml_path)
            workflow_name, steps_config, metadata = parse_yaml(yaml_path)

            functions_dir_str = metadata.get("functions_dir", "../steps")
            functions_dir = (yaml_path.parent / functions_dir_str).resolve()
            verbose = metadata.get("verbose", 2)

            phases = split_phases(steps_config)
            levels = metadata.get("levels")
            if levels is not None and (
                not isinstance(levels, list)
                or not all(isinstance(level, str) for level in levels)
                or len(set(levels)) != len(levels)
            ):
                raise ValueError(
                    "metadata 'levels' must be a list of distinct level "
                    f"names, widest first, got {levels!r}"
                )

            # Read METADATA from all step files
            step_settings = {}
            for phase in phases:
                for step in phase.steps:
                    if step.name not in step_settings:
                        step_path = functions_dir / f"{step.name}.py"
                        settings = get_step_settings(step_path)
                        # A step naming the engine's own environment runs
                        # on the engine's Python: None, like a step that
                        # names none.
                        env = settings["environment"]
                        if env is not None and env == self._default_env:
                            env = None
                        settings["environment"] = env
                        if step.max_workers is not None:
                            settings["max_workers"] = step.max_workers
                        if (
                            not isinstance(settings["max_workers"], int)
                            or settings["max_workers"] < 1
                        ):
                            raise ValueError(
                                f"Step '{step.name}': max_workers must be a "
                                f"whole number of 1 or more, got "
                                f"{settings['max_workers']!r}"
                            )
                        step_settings[step.name] = settings

            state = PipelineState(
                name=name,
                yaml_path=yaml_path,
                phases=phases,
                functions_dir=functions_dir,
                step_settings=step_settings,
                verbose=verbose,
                levels=levels,
                workflow_name=workflow_name,
            )

            with self._lock:
                if not self._accepting:
                    raise RuntimeError("Engine has been shut down")
                self._pipelines[name] = state
        finally:
            with self._lock:
                self._registering.discard(name)

        logger.info(
            "Registered pipeline '%s': workflow=%s, %d phases, %d steps",
            name,
            workflow_name,
            len(phases),
            sum(len(p.steps) for p in phases),
        )

    def submit(self, pipeline, data, scope=None, priority=None, complete=None):
        """
        Submit one image to a registered pipeline. Returns its job id at once.

        Parameters
        ----------
        pipeline : str
            The pipeline's registered name.
        data : dict
            What the steps see as ``pipeline_data["input"]``.
        scope : dict, optional
            The units this image belongs to,
            e.g. {"carrier": 1, "compartment": 3, "group": 2}.
        priority : int, optional
            Higher runs first. Equal priorities run in submission order.
        complete : str or list, optional
            The level, or levels, this image closes for its unit. Each
            level the pipeline has a step for adds a scope job to the board.

        Returns
        -------
        int
            The image job's id on the board, counted per pipeline from 1.

        Raises
        ------
        ScopeError
            A level in ``complete`` is a scope of this pipeline, this scope
            leaves out its key, and earlier submits used that key: the
            signal would close every unit of that level at once.
        """
        scope = scope or {}
        data = data if data is not None else {}
        priority_value = priority if priority is not None else 0

        # Keep acceptance, executor submission, and optional scope submission
        # atomic with shutdown. Once shutdown acquires this lock and closes
        # acceptance, no partially-accounted submission can reach an executor.
        with self._lock:
            if not self._accepting:
                raise RuntimeError("Engine has been shut down")
            if pipeline not in self._pipelines:
                raise KeyError(f"Pipeline '{pipeline}' is not registered")
            state = self._pipelines[pipeline]
            complete_levels = (
                [] if not complete else [complete] if isinstance(complete, str) else list(complete)
            )
            state.check_complete(complete_levels, scope)
            job = state.new_image_job(scope)
            state.note_open(scope)

            # Submit Phase 0 to thread pool
            future = self._executor.submit(
                self._execute_phase0,
                state,
                data,
                scope,
                job,
                priority=priority_value,
            )
            state.add_job_entry(future, scope, job)

            # Handle scope completion signals
            if complete_levels:
                # Completing a level also closes the narrower units of it
                # still open, narrowest first.
                signals = state.close_signals(complete_levels, scope)
                # Each close is marked as running here, synchronously, so a
                # later signal for a wider scope (a carrier) cannot overtake
                # a narrower one (a compartment) submitted before it.
                phases = [state.get_triggered_phase_idx(level) for level, _ in signals]
                tokens = [
                    state.begin_scoped(k, unit_scope, job)
                    for k, (_, unit_scope) in zip(phases, signals, strict=True)
                ]
                # One scope job per close the pipeline has a step for.
                scope_jobs = [
                    None
                    if k is None
                    else state.new_scope_job(k, state.scope_key(level, unit_scope))
                    for k, (level, unit_scope) in zip(phases, signals, strict=True)
                ]
                # Process the closes sequentially in one thread so that
                # chained scopes (e.g., ["group", "all"]) execute in order.
                chain = self._scope_executor.submit(
                    self._handle_scope_complete_chain,
                    state,
                    signals,
                    tokens,
                    scope_jobs,
                    job,
                )
                state.add_future(chain)
                # A chain cancelled before it ran clears its marks here, or a
                # waiter would hang, and its scope jobs are cancelled.
                chain.add_done_callback(
                    lambda done: (
                        [state.end_scoped(t) for t in tokens]
                        + [state.job_cancelled(j) for j in scope_jobs if j is not None]
                        if done.cancelled()
                        else None
                    )
                )
        return job

    def status(self, name=None):
        """
        The job board of a pipeline, or of every pipeline.

        Parameters
        ----------
        name : str, optional
            The pipeline's name. If None, one board per pipeline, by name.

        Returns
        -------
        dict
            Job id -> ``step``, ``scope``, ``state`` (queued, running,
            waiting, done, failed, cancelled) and ``output``; a failed job
            also has ``error``.
        """
        if name is not None:
            return self._get_pipeline(name).board()

        with self._lock:
            states = dict(self._pipelines)
        return {n: s.board() for n, s in states.items()}

    def deregister(self, name, wait=True):
        """
        End a pipeline and stop the workers no other pipeline needs.

        The pipeline takes no new images. With ``wait`` its queued and running
        jobs finish first; without, running steps are killed and the jobs
        that did not finish are cancelled. Then the workers of every
        environment no other registered pipeline uses are stopped, and the
        name can be registered again.

        Returns
        -------
        dict
            The pipeline's final board.
        """
        with self._lock:
            if name not in self._pipelines:
                raise KeyError(f"Pipeline '{name}' is not registered")
            state = self._pipelines.pop(name)
        logger.info("Deregistering pipeline '%s' (wait=%s)", name, wait)

        if not wait:
            state.cancelling = True
            for future in list(state.futures):
                future.cancel()
            self._pool.kill_owner(state)
        concurrent.futures.wait(list(state.futures))
        board = state.board()

        # Taken under the lock, so a pipeline registered meanwhile that needs
        # one of these environments gets fresh workers, not these.
        with self._lock:
            in_use = set()
            for other in self._pipelines.values():
                in_use |= other.environments
            leaving = [self._pool.take_environment(env) for env in state.environments - in_use]
        for pool in leaving:
            if pool is not None:
                pool.shutdown_all(now=not wait)
        return board

    def shutdown(self, wait=True):
        """Deregister every pipeline and stop the engine and all workers.

        Parameters
        ----------
        wait : bool, optional
            If True, let the queued jobs finish first. If False, kill the
            running steps at once and cancel the rest, as
            ``deregister(name, wait=False)`` does for every pipeline.

        Returns
        -------
        None
        """
        logger.info("Engine shutting down (wait=%s)", wait)
        with self._lock:
            self._accepting = False
            names = list(self._pipelines)
        for name in names:
            try:
                self.deregister(name, wait=wait)
            except KeyError:
                pass  # deregistered by another thread meanwhile
        self._executor.shutdown(wait=wait)
        self._scope_executor.shutdown(
            wait=wait,
            cancel_futures=not wait,
        )
        self._pool.shutdown_all(now=not wait)
        logger.debug("Engine shutdown complete")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.shutdown()
        return False

    # -- Internal: scope completion chain --------------------------------

    def _handle_scope_complete_chain(self, state, signals, tokens, jobs, submission_idx):
        """Process a submit's closes sequentially, each ``(level, scope)``.

        Each close must complete before the next starts, so chained
        scopes like ["group", "all"] execute in the correct order. Every
        close's running mark is cleared when it finishes, whatever happens,
        and its scope job is never left waiting.
        """
        for (level, scope), token, job in zip(signals, tokens, jobs, strict=True):
            try:
                self._handle_scope_complete(state, level, scope, submission_idx, job)
            except Exception as e:
                logger.error("Scope chain failed at level '%s': %s", level, e)
                if job is not None:
                    state.job_failed(job, str(e))
            finally:
                state.end_scoped(token)

    # -- Internal: Phase 0 execution -----------------------------------

    def _execute_phase0(self, state, input_data, scope, submission_idx):
        """Execute Phase 0 (immediate steps) for one image job. The job's id
        is its submission index."""
        phase = state.phases[0]

        pipeline_data = {
            "metadata": {
                "datetime": datetime.now().strftime("%Y%m%d-%H%M%S"),
                "workflow_name": state.workflow_name,
                "yaml_filename": state.yaml_path.name,
                "steps": [s.name for s in phase.steps],
                "verbose": state.verbose,
                "scope": scope,
                "submission_idx": submission_idx,
            },
            "input": input_data,
        }

        step_name = "unknown"
        try:
            for step in phase.steps:
                step_name = step.name
                pipeline_data = self._execute_step(state, step, pipeline_data, submission_idx)

                if not isinstance(pipeline_data, dict):
                    raise TypeError(
                        f"Step '{step.name}' returned {type(pipeline_data).__name__}, expected dict"
                    )

            # Store result for scope collection if next phase is scoped
            if len(state.phases) > 1 and state.phases[1].scope is not None:
                state.store_phase0_result(submission_idx, scope, pipeline_data)

            state.job_done(submission_idx, dict(pipeline_data))

        except Exception as e:
            state.record_failure(scope, step_name, str(e), 0, submission_idx)
            state.job_failed(submission_idx, str(e), step_name)
            logger.error(
                "Phase 0 failed in '%s' (submission %d, step %s): %s",
                state.name,
                submission_idx,
                step_name,
                e,
            )
            raise

    # -- Internal: scope completion ------------------------------------

    @staticmethod
    def _lineage(results, failures):
        """What a scoped result is built from.

        ``submissions`` are the indices of every tile under the unit,
        ``failed`` every failure record under it, and ``provenance`` the
        distinct record of every step below this one: where it ran, on
        which Python and packages. One record per step is the norm; two
        mean the environment changed during the run.
        """
        submissions, failed, provenance, seen = [], list(failures), {}, set()

        def add(step, record):
            # Records are deduplicated on their JSON text.
            key = (step, json.dumps(record, sort_keys=True, default=str))
            if key not in seen:
                seen.add(key)
                provenance.setdefault(step, []).append(record)

        for r in results:
            below = r.get("lineage")
            if below:
                submissions += below["submissions"]
                failed += below["failed"]
                for step, records in below["provenance"].items():
                    for record in records:
                        add(step, record)
            else:
                idx = r.get("metadata", {}).get("submission_idx")
                if idx is not None:
                    submissions.append(idx)
            for step, record in r.get("provenance", {}).items():
                add(step, record)
        return {"submissions": sorted(submissions), "failed": failed, "provenance": provenance}

    def _handle_scope_complete(self, state, level, scope, submission_idx, job):
        """Handle a scope completion signal for scope job *job*.

        Waits for the unit's Phase 0 jobs submitted up to this signal to
        finish, collects results, and executes the triggered scoped phase.
        """
        phase_idx = state.get_triggered_phase_idx(level)
        if phase_idx is None:
            logger.warning("No phase with scope '%s' in pipeline '%s'", level, state.name)
            return

        # The unit being closed: this level's value and every wider level's,
        # since compartment 3 exists on every carrier. None means everything.
        unit = state.scope_key(level, scope)

        # Wait for all matching Phase 0 futures to complete
        matching_futures = state.get_matching_futures(unit, submission_idx)
        for f in matching_futures:
            try:
                f.result()
            except Exception:
                pass  # Failures already recorded by Phase 0 handler

        # Collect results from previous phase, and what a narrower level
        # never closed
        results, failures = state.collect_for_scope(phase_idx, unit, submission_idx)
        failures += state.held_for(phase_idx, unit, submission_idx)

        if state.cancelling:
            state.job_cancelled(job)
            return

        if not results and not failures:
            logger.warning("No results for scope '%s' (unit=%s) in '%s'", level, unit, state.name)
            state.job_failed(job, f"no images in this {level}")
            return

        # Clean up consumed job entries
        state.cleanup_consumed_entries(unit, submission_idx)

        # Execute the scoped phase. The lineage is attached after the step
        # returns, so a step that builds a new dict cannot drop it; the
        # same for the phase's metadata.
        lineage = self._lineage(results, failures)
        try:
            result, metadata = self._execute_scoped_phase(
                state, phase_idx, results, failures, scope, level, unit or {}, job
            )
            result.setdefault("metadata", metadata)
            result["lineage"] = lineage

            # Store for next phase if there is one
            next_phase = phase_idx + 1
            if next_phase < len(state.phases):
                state.store_phase_result(phase_idx, result, scope, submission_idx)

            state.job_done(job, dict(result))

        except Exception as e:
            step_name = getattr(e, "step", None)
            state.record_failure(scope, step_name, str(e), phase_idx, submission_idx)
            state.job_failed(job, str(e), step_name)
            logger.error(
                "Phase %d failed in '%s' (submission %d, step %s): %s",
                phase_idx,
                state.name,
                submission_idx,
                step_name,
                e,
            )

    def _execute_scoped_phase(
        self, state, phase_idx, accumulated_results, failures, scope, scope_level, unit, job
    ):
        """Execute a scoped phase with accumulated results. ``unit`` is the
        unit being closed, widest level first, ``{}`` for everything. An
        exception from a step carries the step's name as ``.step``.
        Returns the last step's result and the phase's metadata."""
        phase = state.phases[phase_idx]

        metadata = {
            "datetime": datetime.now().strftime("%Y%m%d-%H%M%S"),
            "workflow_name": state.workflow_name,
            "yaml_filename": state.yaml_path.name,
            "steps": [s.name for s in phase.steps],
            "phase": phase_idx,
            "scope_level": scope_level,
            "scope": scope,
            "unit": unit,
            "n_accumulated": len(accumulated_results),
            "n_failures": len(failures),
            "verbose": state.verbose,
        }
        pipeline_data = {
            "results": accumulated_results,
            "failures": failures,
            "metadata": metadata,
        }

        for step in phase.steps:
            try:
                pipeline_data = self._execute_step(state, step, pipeline_data, job)
                if not isinstance(pipeline_data, dict):
                    raise TypeError(
                        f"Step '{step.name}' returned {type(pipeline_data).__name__}, expected dict"
                    )
            except Exception as e:
                e.step = step.name
                raise
            # A step that built a new dict still hands the next one the
            # phase's metadata.
            pipeline_data.setdefault("metadata", metadata)

        return pipeline_data, metadata

    # -- Internal: step execution --------------------------------------

    def _execute_step(self, state, step_config, pipeline_data, job):
        """Execute a single step of *job* via the worker pool.

        The board shows the job at this step from here on. A pipeline being
        cancelled starts no further step.
        """
        if state.cancelling:
            raise RuntimeError("cancelled by deregister")
        state.job_at(job, step_config.name)
        settings = state.step_settings[step_config.name]
        func_path = state.functions_dir / f"{step_config.name}.py"

        return self._pool.execute(
            environment=settings["environment"],
            step_path=str(func_path),
            pipeline_data=pipeline_data,
            params=step_config.params,
            max_workers=settings["max_workers"],
            timeout=self.execution_timeout,
            owner=state,
        )

    # -- Internal: helpers ---------------------------------------------

    def _get_pipeline(self, name):
        """Get a registered pipeline state or raise."""
        with self._lock:
            if name not in self._pipelines:
                raise KeyError(f"Pipeline '{name}' is not registered")
            return self._pipelines[name]

    def __repr__(self):
        with self._lock:
            n = len(self._pipelines)
        return f"Engine(pipelines={n}, pool={self._pool!r})"


def get_step_settings(step_path: Path) -> dict:
    """
    Extract execution settings from a step file without running it.

    Parses the file's AST to read the METADATA dict literal.
    No module code is executed.

    Returns
    -------
    dict
        - environment : str or None
            Conda environment name. None means orchestrator's environment.
        - max_workers : int
            Maximum parallel workers for this step. Default 1.
    """
    metadata = _extract_metadata(step_path) or {}
    settings = {
        "environment": metadata.get("environment", None),
        "max_workers": metadata.get("max_workers", 1),
    }
    logger.debug(
        "Step settings for %s: environment=%s, max_workers=%d",
        step_path.name,
        settings["environment"],
        settings["max_workers"],
    )
    return settings


def _extract_metadata(step_path: Path) -> dict:
    """Extract the METADATA dict literal from a step file via AST."""
    with open(step_path) as f:
        tree = ast.parse(f.read())

    for node in ast.iter_child_nodes(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "METADATA"
        ):
            result = ast.literal_eval(node.value)
            logger.debug(
                "Extracted METADATA from %s (line %d): %s", step_path.name, node.lineno, result
            )
            return result

    logger.debug("No METADATA found in %s, using defaults", step_path.name)
    return {}
