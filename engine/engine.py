"""
Engine -- what you call to run analyses.

You register a recipe, submit images to it, and collect the results. The
Engine never runs step code itself: it reads each step's METADATA without
running the file, and hands the work to workers (see workers.py).

Engine -- Central orchestrator for the v4 pipeline engine.
----------------------------------------------------------
Four core methods plus shutdown:

    engine = Engine()
    engine.register("overview", "path/to/overview.yaml")
    engine.submit("overview", data, scope={"group": "R3"}, complete="group")
    engine.status("overview")
    engine.results("overview")
    engine.shutdown()

The engine never executes step code. All step execution happens in worker
subprocesses managed by the WorkerPool. Step files are only read via AST
at register() time to extract METADATA.

Thread safety
-------------
- _lock protects _pipelines dict and _accepting flag.
- Each PipelineState has its own lock for internal state.
- submit() and results() can be called from different threads safely.


Step METADATA extraction via AST parsing.
-----------------------------------------
Reads a step file's METADATA dict without executing any code. Used by the
engine at register() time to determine execution requirements for each step.

The engine never imports or executes step files. All step execution happens
in worker subprocesses running in the correct conda environment.
"""

from __future__ import annotations

import ast
import heapq
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
                raise RuntimeError("pool is shut down")
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
    Central pipeline orchestrator.

    Parameters
    ----------
    idle_timeout : float or None
        Seconds before idle workers are shut down (default: 300). None
        keeps them for as long as the engine lives, for a caller that holds
        one engine for a session and wants the workers' imports paid once.
    max_concurrent : int
        Maximum concurrent operations in the thread pool (default: 8).
    execution_timeout : float or None
        Default timeout for a single step in seconds (default: 300).
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
        runs in the environment its file names unless the YAML names one
        for it; either way an environment that is the orchestrator's own
        becomes None, the orchestrator's interpreter.

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
                    or not all(isinstance(l, str) for l in levels)
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
            )
            state.workflow_name = workflow_name

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
        Submit a job to a registered pipeline. Non-blocking.

        Parameters
        ----------
        name : str
            Registered pipeline name.
        data : dict
            Input data for the pipeline.
        scope : dict, optional
            Labels which scope group this job belongs to.
            E.g., {"carrier": 1, "compartment": 3, "group": 2}.
        priority : int, optional
            Higher = more urgent. Default is FIFO (submission order).
        complete : str or list, optional
            Signals that one or more scope levels are complete for this
            job's scope group.
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
        Query pipeline status.

        Parameters
        ----------
        name : str, optional
            Pipeline name. If None, returns status for all pipelines.

        Returns
        -------
        dict
            Pipeline status with pending, running, completed, failed counts
            and failure details.
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
            If True, wait for queued engine tasks to finish before the
            thread pool returns. If False, put the workers down first,
            busy ones included, so that a step in flight dies now and the
            engine thread waiting on it is released; nothing queued runs.
            This is the operator's Interrupt: measured before it, a stop
            pressed one second into a tile test waited 19 s for the field
            and then handed its objects back, because the threads were
            joined before the workers were touched.

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
        for level, token in zip(levels, tokens):
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
            logger.error("Phase 0 failed for %s (idx=%d): %s",
                         state.name, submission_idx, e)
            raise

    # -- Internal: scope completion ------------------------------------

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
        # since compartment 3 exists on every carrier.
        value = state.scope_key(level, scope)

        # Wait for all matching Phase 0 futures to complete
        matching_futures = state.get_matching_futures(value, submission_idx)
        for f in matching_futures:
            try:
                f.result()
            except Exception:
                pass  # Failures already recorded by Phase 0 handler

        # Collect results from previous phase, and what a narrower level
        # never closed
        results, failures = state.collect_for_scope(
            phase_idx, value, submission_idx)
        failures += state.held_for(phase_idx, value, submission_idx)

        if not results and not failures:
            logger.warning("No results for scope '%s' (value=%s) in '%s'",
                           level, value, state.name)
            return

        # Clean up consumed job entries
        state.cleanup_consumed_entries(value, submission_idx)

        # Execute the scoped phase
        state.record_start(is_submission=False)
        step_name = [None]
        try:
            result = self._execute_scoped_phase(
                state, phase_idx, results, failures, scope, level,
                value or {}, step_name)

            # Store for next phase if there is one
            next_phase = phase_idx + 1
            if next_phase < len(state.phases):
                state.store_phase_result(
                    phase_idx, result, scope, submission_idx)

            # Publish scoped result
            state.publish_result(dict(result), phase_idx, scope, level)
            state.record_completion()

        except Exception as e:
            state.record_failure(
                scope, step_name[0], str(e), phase_idx, submission_idx)
            logger.error("Scoped phase %d failed for %s: %s",
                         phase_idx, state.name, e)

    def _execute_scoped_phase(self, state, phase_idx, accumulated_results,
                               failures, scope, scope_level, unit, step_name):
        """Execute a scoped phase with accumulated results. ``unit`` is the
        unit being closed, widest level first, ``{}`` for everything. The
        step being run is written to ``step_name[0]`` so a failure can
        name it."""
        phase = state.phases[phase_idx]

        pipeline_data = {
            "results": accumulated_results,
            "failures": failures,
            "metadata": {
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
            },
        }

        for step in phase.steps:
            step_name[0] = step.name
            pipeline_data = self._execute_step(state, step, pipeline_data)

            if not isinstance(pipeline_data, dict):
                raise TypeError(
                    f"Step '{step.name}' returned "
                    f"{type(pipeline_data).__name__}, expected dict"
                )

        return pipeline_data

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
