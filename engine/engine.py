"""
Engine -- what you call to run analyses.

You register a recipe, submit images to it, and collect the results. The
Engine never runs step code itself: it reads each step's METADATA without
running the file, and hands the work to workers (see workers.py). Scope
handling, phases and per-recipe state live in pipeline.py.

Thread safety: ``_lock`` guards the registered recipes and the accepting
flag; each PipelineState has its own lock. ``submit`` and ``results`` may
be called from different threads.
"""

from __future__ import annotations

import ast
import heapq
import json
import logging
import os
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from .pipeline import PipelineState, parse_yaml, split_phases
from .workers import WorkerPool

logger = logging.getLogger(__name__)


class _PriorityThreadPool:
    """Thread pool that dispatches tasks in priority order.

    Higher priority dispatches first; FIFO within the same priority.
    Compatible subset of ThreadPoolExecutor: ``submit()`` returns a
    ``concurrent.futures.Future``; ``shutdown(wait=True)`` drains workers.
    """

    def __init__(self, max_workers):
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        self._heap = []
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)
        self._counter = 0
        self._shutdown = False
        self._workers = []
        for i in range(max_workers):
            t = threading.Thread(
                target=self._run, name=f"engine-worker-{i}", daemon=True,
            )
            self._workers.append(t)
            t.start()

    def submit(self, fn, *args, priority=0, **kwargs):
        future = Future()
        with self._not_empty:
            if self._shutdown:
                raise RuntimeError("Priority pool has been shut down")
            self._counter += 1
            heapq.heappush(
                self._heap,
                (-priority, self._counter, future, fn, args, kwargs),
            )
            self._not_empty.notify()
        return future

    def _run(self):
        while True:
            with self._not_empty:
                while not self._heap and not self._shutdown:
                    self._not_empty.wait()
                if not self._heap:
                    return
                _, _, future, fn, args, kwargs = heapq.heappop(self._heap)
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(fn(*args, **kwargs))
            except BaseException as e:
                future.set_exception(e)

    def shutdown(self, wait=True):
        to_cancel = []
        with self._not_empty:
            self._shutdown = True
            if not wait:
                to_cancel = [item[2] for item in self._heap]
                self._heap.clear()
            self._not_empty.notify_all()
        for future in to_cancel:
            future.cancel()
        if wait:
            for t in self._workers:
                t.join()


class Engine:
    """
    Runs recipes: register, submit, results, status, shutdown.

    Parameters
    ----------
    idle_timeout : float or None
        Seconds before idle workers are shut down (default: 300). None
        keeps them for as long as the engine lives, for a caller that holds
        one engine for a session and wants the workers' imports paid once.
    max_concurrent : int
        How many jobs run at once (default: 8). Per-image work and scope
        closes each get a thread pool of this size.
    execution_timeout : float or None
        Seconds one step may run before it is killed (default: 300).
    """

    def __init__(self, idle_timeout=300.0, max_concurrent=8,
                 execution_timeout=300.0):
        self.execution_timeout = execution_timeout
        self._pool = WorkerPool(idle_timeout=idle_timeout)
        self._executor = _PriorityThreadPool(max_workers=max_concurrent)
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
        self._default_env = (
            os.environ.get("CONDA_DEFAULT_ENV")
            or Path(sys.prefix).name
        )

        logger.debug("Engine created: idle_timeout=%s, "
                     "max_concurrent=%d, default_env=%s",
                     idle_timeout, max_concurrent, self._default_env)

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
                    or len(set(levels)) != len(levels)):
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
                        if (not isinstance(settings["max_workers"], int)
                                or settings["max_workers"] < 1):
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

        logger.info("Registered pipeline '%s': workflow=%s, %d phases, "
                     "%d steps", name, workflow_name, len(phases),
                     sum(len(p.steps) for p in phases))

    def submit(self, name, data, scope=None, priority=None, complete=None):
        """
        Submit one image to a registered recipe. Returns at once.

        Parameters
        ----------
        name : str
            The recipe's registered name.
        data : dict
            What the steps see as ``pipeline_data["input"]``.
        scope : dict, optional
            The units this image belongs to,
            e.g. {"carrier": 1, "compartment": 3, "group": 2}.
        priority : int, optional
            Higher runs first. Equal priorities run in submission order.
        complete : str or list, optional
            The level, or levels, this image closes for its unit.

        Raises
        ------
        ScopeError
            A level in ``complete`` is a scope of this recipe, this scope
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
            if name not in self._pipelines:
                raise KeyError(f"Pipeline '{name}' is not registered")
            state = self._pipelines[name]
            complete_levels = (
                [] if not complete
                else [complete] if isinstance(complete, str)
                else list(complete)
            )
            state.check_complete(complete_levels, scope)
            submission_idx = state.next_submission_idx()

            # Submit Phase 0 to thread pool
            future = self._executor.submit(
                self._execute_phase0, state, data, scope, submission_idx,
                priority=priority_value,
            )
            state.add_job_entry(future, scope, submission_idx)

            # Handle scope completion signals
            if complete_levels:
                # Each level is marked as running here, synchronously, so a
                # later signal for a wider scope (a carrier) cannot overtake
                # a narrower one (a compartment) submitted before it.
                tokens = [
                    state.begin_scoped(
                        state.get_triggered_phase_idx(level), scope,
                        submission_idx)
                    for level in complete_levels
                ]
                # Process levels sequentially in one thread so that
                # chained scopes (e.g., ["group", "all"]) execute in order.
                chain = self._scope_executor.submit(
                    self._handle_scope_complete_chain, state,
                    complete_levels, scope, tokens, submission_idx,
                )
                # A chain cancelled by shutdown(wait=False) never runs, so
                # its marks are cleared here, or a waiter would hang.
                chain.add_done_callback(
                    lambda done: [state.end_scoped(t) for t in tokens]
                    if done.cancelled() else None
                )

    def status(self, name=None):
        """
        Query a recipe's status.

        Parameters
        ----------
        name : str, optional
            The recipe's name. If None, the status of every recipe.

        Returns
        -------
        dict
            ``pending``, ``running``, ``completed`` and ``failed`` counts,
            the ``failures`` themselves, and ``held``: the results waiting
            for a unit to close.
        """
        if name is not None:
            state = self._get_pipeline(name)
            return state.status

        with self._lock:
            return {
                n: s.status for n, s in self._pipelines.items()
            }

    def results(self, name):
        """
        Retrieve completed results for a pipeline.

        Results are consumed on retrieval. Calling results() again returns
        only new results accumulated since the last call.

        Parameters
        ----------
        name : str
            Pipeline name.

        Returns
        -------
        list of dict
            Completed pipeline_data dicts, each tagged with _phase,
            _scope, and _scope_level metadata.
        """
        state = self._get_pipeline(name)
        return state.drain_results()

    def shutdown(self, wait=True):
        """Shut down the engine, thread pool, and all workers.

        Parameters
        ----------
        wait : bool, optional
            If True, wait for queued jobs to finish, then stop the
            workers. If False, kill the workers first, busy ones included,
            so a step in flight dies now and the thread waiting on it is
            released; nothing queued runs. The workers go before the
            threads are joined, or a stop would wait out the step.

        Returns
        -------
        None
        """
        logger.info("Engine shutting down (wait=%s)", wait)
        with self._lock:
            self._accepting = False
        if not wait:
            self._pool.shutdown_all(now=True)
        self._executor.shutdown(wait=wait)
        self._scope_executor.shutdown(
            wait=wait,
            cancel_futures=not wait,
        )
        if wait:
            self._pool.shutdown_all()
        logger.debug("Engine shutdown complete")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.shutdown()
        return False

    # -- Internal: scope completion chain --------------------------------

    def _handle_scope_complete_chain(self, state, levels, scope, tokens,
                                     submission_idx):
        """Process multiple scope completion levels sequentially.

        Each level must complete before the next starts, so chained
        scopes like ["group", "all"] execute in the correct order. Every
        level's running mark is cleared when it finishes, whatever happens.
        """
        for level, token in zip(levels, tokens, strict=True):
            try:
                self._handle_scope_complete(state, level, scope, submission_idx)
            except Exception as e:
                logger.error("Scope chain failed at level '%s': %s",
                             level, e)
            finally:
                state.end_scoped(token)

    # -- Internal: Phase 0 execution -----------------------------------

    def _execute_phase0(self, state, input_data, scope, submission_idx):
        """Execute Phase 0 (immediate steps) for one job."""
        state.record_start()
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
                pipeline_data = self._execute_step(
                    state, step, pipeline_data)

                if not isinstance(pipeline_data, dict):
                    raise TypeError(
                        f"Step '{step.name}' returned "
                        f"{type(pipeline_data).__name__}, expected dict"
                    )

            # Store result for scope collection if next phase is scoped
            if len(state.phases) > 1 and state.phases[1].scope is not None:
                state.store_phase0_result(submission_idx, scope,
                                          pipeline_data)

            # Publish Phase 0 result
            state.publish_result(dict(pipeline_data), 0, scope, None)
            state.record_completion()

        except Exception as e:
            state.record_failure(scope, step_name, str(e), 0, submission_idx)
            logger.error("Phase 0 failed in '%s' (submission %d, step %s): %s",
                         state.name, submission_idx, step_name, e)
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
        return {"submissions": sorted(submissions), "failed": failed,
                "provenance": provenance}

    def _handle_scope_complete(self, state, level, scope, submission_idx):
        """Handle a scope completion signal.

        Waits for the unit's Phase 0 jobs submitted up to this signal to
        finish, collects results, and executes the triggered scoped phase.
        """
        phase_idx = state.get_triggered_phase_idx(level)
        if phase_idx is None:
            logger.warning("No phase with scope '%s' in pipeline '%s'",
                           level, state.name)
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
        results, failures = state.collect_for_scope(
            phase_idx, unit, submission_idx)
        failures += state.held_for(phase_idx, unit, submission_idx)

        if not results and not failures:
            logger.warning("No results for scope '%s' (unit=%s) in '%s'",
                           level, unit, state.name)
            return

        # Clean up consumed job entries
        state.cleanup_consumed_entries(unit, submission_idx)

        # Execute the scoped phase. The lineage is attached after the step
        # returns, so a step that builds a new dict cannot drop it; the
        # same for the phase's metadata.
        lineage = self._lineage(results, failures)
        state.record_start(is_submission=False)
        try:
            result, metadata = self._execute_scoped_phase(
                state, phase_idx, results, failures, scope, level, unit or {})
            result.setdefault("metadata", metadata)
            result["lineage"] = lineage

            # Store for next phase if there is one
            next_phase = phase_idx + 1
            if next_phase < len(state.phases):
                state.store_phase_result(
                    phase_idx, result, scope, submission_idx)

            # Publish scoped result
            state.publish_result(dict(result), phase_idx, scope, level)
            state.record_completion()

        except Exception as e:
            step_name = getattr(e, "step", None)
            state.record_failure(
                scope, step_name, str(e), phase_idx, submission_idx)
            logger.error("Phase %d failed in '%s' (submission %d, step %s): %s",
                         phase_idx, state.name, submission_idx, step_name, e)

    def _execute_scoped_phase(self, state, phase_idx, accumulated_results,
                               failures, scope, scope_level, unit):
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
                pipeline_data = self._execute_step(state, step, pipeline_data)
                if not isinstance(pipeline_data, dict):
                    raise TypeError(
                        f"Step '{step.name}' returned "
                        f"{type(pipeline_data).__name__}, expected dict"
                    )
            except Exception as e:
                e.step = step.name
                raise
            # A step that built a new dict still hands the next one the
            # phase's metadata.
            pipeline_data.setdefault("metadata", metadata)

        return pipeline_data, metadata

    # -- Internal: step execution --------------------------------------

    def _execute_step(self, state, step_config, pipeline_data):
        """Execute a single step via the worker pool."""
        settings = state.step_settings[step_config.name]
        func_path = state.functions_dir / f"{step_config.name}.py"

        return self._pool.execute(
            environment=settings["environment"],
            step_path=str(func_path),
            pipeline_data=pipeline_data,
            params=step_config.params,
            max_workers=settings["max_workers"],
            timeout=self.execution_timeout,
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
    logger.debug("Step settings for %s: environment=%s, max_workers=%d",
                 step_path.name, settings["environment"],
                 settings["max_workers"])
    return settings


def _extract_metadata(step_path: Path) -> dict:
    """Extract the METADATA dict literal from a step file via AST."""
    with open(step_path) as f:
        tree = ast.parse(f.read())

    for node in ast.iter_child_nodes(tree):
        if (isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "METADATA"):
            result = ast.literal_eval(node.value)
            logger.debug("Extracted METADATA from %s (line %d): %s",
                         step_path.name, node.lineno, result)
            return result

    logger.debug("No METADATA found in %s, using defaults", step_path.name)
    return {}
