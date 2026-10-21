"""
Engine -- what you call to run analyses.

You register a pipeline, submit images to it, and read their output from the
job board. The Engine never runs step code itself: it reads each step's
METADATA without running the file, and hands the work to workers (see
workers.py). A registered pipeline (pipeline.py) has its units (units.py:
what belongs together, and what is complete) and its board (board.py: the
jobs, the run record and the hand-over folders); waiting jobs start in the
order of the job queue (job_queue.py).

A worker lives for as long as a registered pipeline needs it: deregister a
pipeline and the workers no other pipeline needs are stopped.

Every pipeline writes into ``<output>/analysis/<name>/``: a copy of its
YAML, the run record, and the folders one step's output is
handed to the next through (handover.py). Between steps, data goes through
these folders, not through the engine.

Thread safety: ``lock`` guards the registered pipelines and the accepting
flag; a pipeline's units and board have their own locks. ``submit`` and ``status`` may
be called from different threads.
"""

from __future__ import annotations

import ast
import concurrent.futures
import hashlib
import json
import logging
import os
import socket
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .board import described
from .job_queue import Order, OrderedThreadPool, deadline_at
from .pipeline import PipelineState, check_phases, parse_yaml, split_phases, width_of
from .units import Stored
from .workers import WorkerPool

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Close:
    """The close of one unit: which pipeline and phase, the unit, the scope
    job that runs it, and the submit and order it came with."""

    state: PipelineState
    phase_idx: int
    level: str
    #: The scope of the submit that closed the unit.
    scope: dict
    #: The unit, widest level first; ``{}`` for everything.
    unit: dict
    job: int
    submission_idx: int
    order: tuple


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
        self.workers = WorkerPool()
        #: The order key of every job (job_queue.py).
        self.order = Order()
        self.image_pool = OrderedThreadPool(max_concurrent, "engine-image")
        # A close waits for the image jobs of its unit. It runs in a pool of
        # its own, or it could take the last thread those image jobs need.
        self.scope_pool = OrderedThreadPool(max_concurrent, "engine-scope")
        self.pipelines = {}
        self.registering = set()
        self.accepting = True
        self.lock = threading.Lock()

        # Detect orchestrator's conda environment
        self.default_env = os.environ.get("CONDA_DEFAULT_ENV") or Path(sys.prefix).name

        logger.debug(
            "Engine created: max_concurrent=%d, default_env=%s",
            max_concurrent,
            self.default_env,
        )

    # -- Public API ----------------------------------------------------

    def register(self, name, yaml_path, output):
        """
        Register a pipeline by name.

        Parses the YAML, resolves functions_dir, reads METADATA from all
        step files via AST, and builds the internal phase structure. Every
        step must name the environment it runs in; one that names the
        orchestrator's own runs on the orchestrator's interpreter.

        Parameters
        ----------
        name : str
            Short name for the pipeline. Used in all subsequent calls.
        yaml_path : str or Path
            Path to the pipeline YAML file.
        output : str or Path
            Where the pipeline writes: ``<output>/analysis/<name>/``.
        """
        with self.lock:
            if not self.accepting:
                raise RuntimeError("Engine has been shut down")
            if name in self.pipelines or name in self.registering:
                raise ValueError(f"Pipeline '{name}' is already registered")
            self.registering.add(name)

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
            check_phases(phases, levels)

            # Read METADATA from all step files
            step_settings, step_hashes = {}, {}
            for phase in phases:
                for step in phase.steps:
                    if step.name not in step_settings:
                        step_path = functions_dir / f"{step.name}.py"
                        if "environment" not in (metadata_of(step_path) or {}):
                            raise ValueError(
                                f"Step '{step.name}' ({step_path}) names no environment: "
                                'add "environment" to its METADATA'
                            )
                        step_hashes[step.name] = hashlib.sha256(step_path.read_bytes()).hexdigest()
                        settings = get_step_settings(step_path)
                        # A step naming the engine's own environment runs
                        # on the engine's Python: None, like a step that
                        # names none.
                        env = settings["environment"]
                        if env is not None and env == self.default_env:
                            env = None
                        settings["environment"] = env
                        step_settings[step.name] = settings
                    width = width_of(step, step_settings)
                    if isinstance(width, bool) or not isinstance(width, int) or width < 1:
                        raise ValueError(
                            f"Step '{step.name}': max_workers must be a "
                            f"whole number of 1 or more, got {width!r}"
                        )

            state = PipelineState(
                name=name,
                yaml_path=yaml_path,
                phases=phases,
                functions_dir=functions_dir,
                step_settings=step_settings,
                verbose=verbose,
                levels=levels,
                workflow_name=workflow_name,
                output=output,
                registration=f"{datetime.now():%Y%m%d-%H%M%S-%f}-{os.getpid()}@{socket.gethostname()}",
            )
            # Imported here: the package sets its version after importing this module.
            from . import __version__

            yaml_text = yaml_path.read_text(encoding="utf-8")
            try:
                state.board.open(
                    yaml_text,
                    {
                        "pipeline": name,
                        "workflow": state.workflow_name,
                        "yaml": str(yaml_path.resolve()),
                        "yaml_text": yaml_text,
                        "steps": step_hashes,
                        "engine": __version__,
                    },
                )
                with self.lock:
                    if not self.accepting:
                        raise RuntimeError("Engine has been shut down")
                    self.pipelines[name] = state
            except BaseException:
                state.board.abandon()
                raise
        finally:
            with self.lock:
                self.registering.discard(name)

        logger.info(
            "Registered pipeline '%s': workflow=%s, %d phases, %d steps",
            name,
            workflow_name,
            len(phases),
            sum(len(p.steps) for p in phases),
        )

    def submit(self, pipeline, data, scope=None, deadline=None, complete=None):
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
        deadline : float, optional
            Seconds from now by which the microscope needs this image's
            answer. Waiting jobs start nearest deadline first; jobs without
            one come after, in submission order. The board marks a job that
            ended after its deadline ``late``; it is not stopped.
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
        due = deadline_at(deadline)

        # Keep acceptance, executor submission, and optional scope submission
        # atomic with shutdown. Once shutdown acquires this lock and closes
        # acceptance, no partially-accounted submission can reach an executor.
        with self.lock:
            if not self.accepting:
                raise RuntimeError("Engine has been shut down")
            if pipeline not in self.pipelines:
                raise KeyError(f"Pipeline '{pipeline}' is not registered")
            state = self.pipelines[pipeline]
            complete_levels = (
                [] if not complete else [complete] if isinstance(complete, str) else list(complete)
            )
            state.units.check_complete(complete_levels, scope)
            job = state.board.new_image_job(scope, due)
            order = self.order.key(due)
            state.units.note_open(scope)
            state.board.record.write(
                "submit", job=job, scope=scope, complete=complete, data=described(data)
            )

            # A position closes itself when its image is done.
            position_job = None
            if state.position_phase:
                unit = state.units.scope_key("position", scope)
                position_job = state.board.new_scope_job(1, unit, due)
            future = self.image_pool.submit(
                order, self.run_image_job, state, data, scope, job, order, position_job
            )
            state.add_image_job(future, scope, job)

            # Handle scope completion signals
            if complete_levels:
                # Completing a level also closes the narrower units of it
                # still open, narrowest first.
                signals = state.units.close_signals(complete_levels, scope)
                # Each close is marked as running here, synchronously, so a
                # later signal for a wider scope (a carrier) cannot overtake
                # a narrower one (a compartment) submitted before it.
                phases = [state.units.phase_for(level) for level, _ in signals]
                tokens = [
                    state.units.begin_scoped(k, unit_scope, job)
                    for k, (_, unit_scope) in zip(phases, signals, strict=True)
                ]
                # One scope job per close the pipeline has a step for.
                scope_jobs = [
                    None
                    if k is None
                    else state.board.new_scope_job(k, state.units.scope_key(level, unit_scope), due)
                    for k, (level, unit_scope) in zip(phases, signals, strict=True)
                ]
                self.queue_closes(state, signals, tokens, scope_jobs, job, order)
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
            return self.pipeline_named(name).board.view()

        with self.lock:
            states = dict(self.pipelines)
        boards = {}
        for n, s in states.items():
            try:
                boards[n] = s.board.view()
            except KeyError:
                pass  # deregistered meanwhile
        return boards

    def deregister(self, name, wait=True):
        """
        End a pipeline and stop the workers no other pipeline needs.

        The pipeline takes no new images. With ``wait`` its queued and running
        jobs finish first; without, running steps are killed and the jobs
        that did not finish are cancelled. Then the workers of every
        environment no other registered pipeline uses are stopped, and the
        name can be registered again.

        The hand-over files go; the board returned is read before they do.

        Returns
        -------
        dict
            The pipeline's final board.
        """
        return self.end_pipeline(name, wait, keep_board=True)

    def end_pipeline(self, name, wait, keep_board):
        """Deregister *name*; return its final board when *keep_board*."""
        with self.lock:
            if name not in self.pipelines:
                raise KeyError(f"Pipeline '{name}' is not registered")
            state = self.pipelines.pop(name)
        logger.info("Deregistering pipeline '%s' (wait=%s)", name, wait)

        if not wait:
            state.cancelling = True
            for future in list(state.futures):
                future.cancel()
            self.workers.kill_owner(state)
        # A finished image job may queue its position's close, and a finished
        # window starts the one held for it: wait until nothing is left.
        unfinished = state.unfinished()
        while unfinished:
            concurrent.futures.wait(unfinished)
            unfinished = state.unfinished()
        # A window whose timepoint before never ended does not run.
        for job in state.units.windows.still_held():
            state.board.job_cancelled(job)
        try:
            board = state.board.final() if keep_board else None
        finally:
            state.board.close()

        # Taken under the lock, so a pipeline registered meanwhile that needs
        # one of these environments gets fresh workers, not these.
        with self.lock:
            in_use = set()
            for other in self.pipelines.values():
                in_use |= other.environments
            leaving = [self.workers.take_environment(env) for env in state.environments - in_use]
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
        with self.lock:
            self.accepting = False
            names = list(self.pipelines)
        for name in names:
            try:
                self.end_pipeline(name, wait, keep_board=False)
            except KeyError:
                pass  # deregistered by another thread meanwhile
        self.image_pool.shutdown(wait=wait)
        self.scope_pool.shutdown(wait=wait)
        self.workers.shutdown_all(now=not wait)
        logger.debug("Engine shutdown complete")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.shutdown()
        return False

    # -- Closing units -------------------------------------------------

    def queue_closes(self, state, signals, tokens, jobs, submission_idx, order):
        """Queue a submit's closes, to run one after another in one thread,
        so that ["group", "all"] closes the group before everything."""
        closing = self.scope_pool.submit(
            order, self.close_units, state, signals, tokens, jobs, submission_idx, order
        )
        state.add_future(closing)
        # Closes cancelled before they ran clear their marks here, or a
        # waiter would hang, and their scope jobs are cancelled.
        closing.add_done_callback(
            lambda done: (
                [state.units.end_scoped(t) for t in tokens]
                + [state.board.job_cancelled(j) for j in jobs if j is not None]
                if done.cancelled()
                else None
            )
        )

    def close_units(self, state, signals, tokens, jobs, submission_idx, order):
        """Run a submit's closes one after another, each ``(level, scope)``.

        Each close must complete before the next starts, so that
        ["group", "all"] closes the group before everything. Every
        close's running mark is cleared when it finishes, whatever happens,
        and its scope job is never left waiting.
        """
        for (level, scope), token, job in zip(signals, tokens, jobs, strict=True):
            try:
                self.close_unit(state, level, scope, submission_idx, job, order)
            except Exception as e:
                logger.error("Closing level '%s' failed: %s", level, e)
                if job is not None:
                    state.job_failed(job, str(e))
            finally:
                state.units.end_scoped(token)

    # -- Image jobs ----------------------------------------------------

    def run_image_job(self, state, input_data, scope, submission_idx, order, position_job):
        """Execute Phase 0 (immediate steps) for one image job. The job's id
        is its submission index. Done or failed, it then queues the close of
        its position, when the pipeline has a position step."""
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
                "output": str(state.board.folder),
            },
            "input": input_data,
        }

        # The first step gets the data as sent; each next one reads the
        # folder the step before wrote.
        folder, steps = None, []
        try:
            for chain in state.chains[0]:
                files = {
                    "load": folder,
                    "save": state.board.step_folder(submission_idx, chain.first, chain.last),
                }
                summary = self.run_chain(
                    state,
                    chain,
                    None if folder else pipeline_data,
                    submission_idx,
                    files,
                    steps,
                    order,
                )
                folder = files["save"]

            # Store result for scope collection if next phase is scoped
            if len(state.phases) > 1 and state.phases[1].scope is not None:
                stored = Stored(folder, submission_idx, scope, summary)
                state.units.store_result(0, stored, scope, submission_idx)

            state.board.job_done(submission_idx, folder, steps)

        except Exception as e:
            step_name = getattr(e, "step", None) or phase.steps[0].name
            failed = (submission_idx, scope)
            state.units.record_failure(failed, step_name, str(e), 0, scope, submission_idx)
            state.job_failed(submission_idx, str(e), step_name, steps)
            logger.error(
                "Phase 0 failed in '%s' (submission %d, step %s): %s",
                state.name,
                submission_idx,
                step_name,
                e,
            )
            raise
        finally:
            if position_job is not None:
                token = state.units.begin_scoped(None, scope, submission_idx)
                signal = ("position", scope)
                self.queue_closes(state, [signal], [token], [position_job], submission_idx, order)

    @staticmethod
    def lineage_of(stored, failures):
        """What a scoped result is built from.

        ``submissions`` are the ids of every image job under the unit,
        ``failed`` every failure under it, and ``provenance`` the distinct
        record of every step below this one: where it ran, on which Python
        and packages. One record per step is the norm; two mean the
        environment changed during the run.
        """
        submissions, failed, provenance, seen = [], list(failures), {}, set()

        def add(step, record):
            # Records are deduplicated on their JSON text.
            key = (step, json.dumps(record, sort_keys=True, default=str))
            if key not in seen:
                seen.add(key)
                provenance.setdefault(step, []).append(record)

        for item in stored:
            below = item.summary.get("lineage")
            if below:
                submissions += below["submissions"]
                failed += below["failed"]
                for step, records in below["provenance"].items():
                    for record in records:
                        add(step, record)
            else:
                submissions.append(item.id)
            for step, record in item.summary.get("provenance", {}).items():
                add(step, record)
        return {"submissions": sorted(submissions), "failed": failed, "provenance": provenance}

    def close_unit(self, state, level, scope, submission_idx, job, order):
        """Handle a scope completion signal for scope job *job*.

        Waits for the unit's Phase 0 jobs submitted up to this signal to
        finish, collects results, and executes the triggered scoped phase.
        """
        phase_idx = state.units.phase_for(level)
        if phase_idx is None:
            logger.warning("No phase with scope '%s' in pipeline '%s'", level, state.name)
            return

        # The unit being closed: this level's value and every wider level's,
        # since compartment 3 exists on every carrier. None means everything.
        unit = state.units.scope_key(level, scope)

        # Wait for all matching Phase 0 futures to complete
        matching_futures = state.units.futures_of(unit, submission_idx)
        for f in matching_futures:
            try:
                f.result()
            except Exception:
                pass  # Failures already recorded by Phase 0 handler

        # Collect the previous phase's outputs, and what a narrower level
        # never closed
        stored, failures = state.units.collect(phase_idx, unit, submission_idx)
        failures += state.units.held_for(phase_idx, unit, submission_idx)

        if state.cancelling:
            state.board.job_cancelled(job)
            return

        if not stored and not failures:
            logger.warning(
                "Nothing to close for scope '%s' (unit=%s) in '%s'", level, unit, state.name
            )
            state.job_failed(job, f"no images in this {level}")
            return

        state.units.forget_entries(unit, submission_idx)
        close = Close(state, phase_idx, level, scope, unit or {}, job, submission_idx, order)

        if state.phases[phase_idx].window:
            windows = state.units.windows
            waits_for = windows.turn(
                phase_idx, unit, stored, job, lambda: self.run_window(close, failures)
            )
            if waits_for is None:
                self.run_window(close, failures)
            else:
                state.board.job_waits(job, waits_for)
            return

        self.run_unit(close, stored, failures, {"parts": [item[:3] for item in stored]})

    def run_window(self, close, failures):
        """Run a window at its timepoint, then the run held for this one.

        The run that ends a timepoint starts the next one of its place, in
        this thread: no thread waits for another.
        """
        windows = close.state.units.windows
        groups, previous = windows.given(close.phase_idx, close.unit)
        stored = [item for _, items in groups for item in items]
        given = {
            "windows": [(t, [item[:3] for item in items]) for t, items in groups],
            "previous": previous,
        }
        folder = None
        try:
            folder = self.run_unit(close, stored, failures, given)
        finally:
            held = windows.finished(close.phase_idx, close.unit, folder)
        if held is not None:
            held()

    def run_unit(self, close, stored, failures, given):
        """Run the scoped phase of one unit, and put the outcome on the board.

        *given* is what its first step reads its parts from. Returns the
        output's folder, or None if it failed.
        """
        state = close.state
        # The lineage comes from what was stored, and the worker adds it to
        # the phase's last output, so a step that builds a new dict cannot
        # drop it.
        lineage = self.lineage_of(stored, failures)
        steps = []
        try:
            folder, summary = self.run_scoped_phase(close, failures, lineage, steps, given)
        except Exception as e:
            step_name = getattr(e, "step", None)
            state.units.record_failure(
                (close.job, close.unit),
                step_name,
                str(e),
                close.phase_idx,
                close.scope,
                close.submission_idx,
            )
            state.job_failed(close.job, str(e), step_name, steps)
            logger.error(
                "Phase %d failed in '%s' (submission %d, step %s): %s",
                close.phase_idx,
                state.name,
                close.submission_idx,
                step_name,
                e,
            )
            return None

        if close.phase_idx + 1 < len(state.phases):
            done = Stored(folder, close.job, close.unit, summary)
            state.units.store_result(close.phase_idx, done, close.scope, close.submission_idx)
        state.board.job_done(close.job, folder, steps, jobs=lineage["submissions"])
        return folder

    def run_scoped_phase(self, close, failures, lineage, steps, given):
        """Execute a scoped phase over the parts of its unit.

        *given* tells the first step where its parts are: ``parts``, or for
        a window ``windows`` and ``previous``. An exception from a step
        carries the step's name as ``.step``. Returns the last step's folder
        and summary.
        """
        state = close.state
        phase = state.phases[close.phase_idx]

        metadata = {
            "datetime": datetime.now().strftime("%Y%m%d-%H%M%S"),
            "workflow_name": state.workflow_name,
            "yaml_filename": state.yaml_path.name,
            "steps": [s.name for s in phase.steps],
            "phase": close.phase_idx,
            "scope_level": close.level,
            "scope": close.scope,
            "unit": close.unit,
            "n_accumulated": len(lineage["submissions"]),
            "n_failures": len(failures),
            "verbose": state.verbose,
            "output": str(state.board.folder),
        }
        pipeline_data = {"parts": None, "failures": failures, "metadata": metadata}

        # The first chain reads its parts from their folders; each next one
        # reads the folder the chain before wrote. Every output keeps the
        # phase's metadata, and the last one gets the lineage.
        chains = state.chains[close.phase_idx]
        folder = None
        for chain in chains:
            files = {
                "load": folder,
                **({} if folder else given),
                "save": state.board.step_folder(close.job, chain.first, chain.last),
                "metadata": metadata,
                "lineage": lineage if chain is chains[-1] else None,
            }
            summary = self.run_chain(
                state,
                chain,
                None if folder else pipeline_data,
                close.job,
                files,
                steps,
                close.order,
            )
            folder = files["save"]

        return folder, summary

    # -- Steps ---------------------------------------------------------

    def run_chain(self, state, chain, pipeline_data, job, files, steps, order):
        """Run a chain of *job* in one call to a worker.

        The board shows the job queued at the chain's first step, running
        once a worker has taken it, and at each step as the worker starts
        it. A pipeline being cancelled starts no further chain. Each step's
        record (its times and provenance) is added to *steps* as it ends,
        for the run record. Returns the output's summary. An error names
        the step it happened in as ``.step``.
        """
        if state.cancelling:
            raise RuntimeError("cancelled by deregister")
        state.board.job_at(job, chain.steps[0].name)

        def report(kind, value):
            if kind == "start":
                state.board.job_step(job, value)
            else:
                steps.append(value)

        return self.workers.execute(
            environment=chain.environment,
            steps=[
                (str(state.functions_dir / f"{step.name}.py"), step.params, width)
                for step, width in zip(chain.steps, chain.widths, strict=True)
            ],
            pipeline_data=pipeline_data,
            timeout=self.execution_timeout,
            owner=state,
            files=files,
            started=lambda: state.board.job_running(job),
            order=order,
            report=report,
        )

    def pipeline_named(self, name):
        """Get a registered pipeline state or raise."""
        with self.lock:
            if name not in self.pipelines:
                raise KeyError(f"Pipeline '{name}' is not registered")
            return self.pipelines[name]

    def __repr__(self):
        with self.lock:
            n = len(self.pipelines)
        return f"Engine(pipelines={n}, pool={self.workers!r})"


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
    metadata = metadata_of(step_path) or {}
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


def metadata_of(step_path: Path) -> dict:
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
