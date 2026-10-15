"""
Pipeline -- how a pipeline is read and how its jobs are tracked.

Not part of the public API; the Engine uses it. It reads a pipeline (YAML),
splits its steps into phases at scope boundaries, keeps track of which
jobs are waiting for which scope to complete, and keeps the job board.

The job board
-------------
Every job has an id, counted per pipeline from 1. A submit makes an image
job; a submit that completes a unit also makes one scope job per level it
closes. Each entry holds the step the job is at, its scope (the unit, for
a scope job), its state and, once done, its output:

    queued -> running -> done | failed          an image job
    waiting -> running -> done | failed         a scope job
    any state before done -> cancelled          deregister(wait=False)

The id doubles as the submission index below: ids only grow, so "submitted
up to and including this one" is still a comparison of numbers.

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
    together with the value of every wider level (see scope_key). The
    wider levels are the pipeline's ``levels`` when it declares them, else
    the scopes of the later phases.
  - If "X" is not a key: collect everything from the previous phase

This means "all" is not special -- it works because no job has "all" as a
scope key, so the engine collects everything.

The same matching holds at every level, not only the first. A compartment
result remembers the scope of the submit that completed it, for example
{"carrier": 1, "compartment": 3}, so a carrier step collects only its own
compartments even while another carrier is still being acquired.

Order
-----
Every submit has an index. A close signal acts on the tiles, unit results
and failures of its unit submitted up to and including itself, and waits
for the ones still running. It does not wait for a signal sent after it:
the signal threads could otherwise all wait on each other.

A wider level completes the narrower ones
-----------------------------------------
Once a unit is complete nothing more can arrive inside it, so completing
a level first closes every narrower unit of it that is still open,
narrowest first: completing carrier 1 closes its open groups, then its
open compartments, then the carrier. A unit is open from the first image
submitted into it until it is closed; a later image opens it again. Which
levels are narrower comes from the pipeline's ``levels`` (widest first, the
experiment above all), or else from the order of its scoped steps.

Up the levels
-------------
A scoped step receives the previous phase's failures of its unit, and what
a narrower level never closed, as failures too. Its result carries a
``lineage``: the tiles under it, the failures under it, and where every
step below it ran. ``ScopeError`` is raised by a close signal whose scope
leaves out the level it closes.
"""

from __future__ import annotations

import logging
import shutil
import threading
import weakref
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import yaml

from .handover import Output, read_folder
from .record import RunRecord

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
    pipeline describes only the analysis: the steps, their order, their
    parameters and their scopes. Which environment each step actually used,
    and the package versions in it, is recorded in every result under
    ``provenance``.
    """

    name: str
    params: dict[str, object]
    #: The pipeline's word on how many of this step may run at once, over
    #: the step file's own METADATA. A step file is written for its heaviest
    #: caller; a pipeline whose work is light (a watershed, not Cellpose)
    #: may run the same file wide without a second copy of it.
    max_workers: int | None = None


@dataclass
class Phase:
    """A group of sequential steps with an optional scope trigger."""

    steps: list[StepConfig]
    scope: str | None = None


class ScopeError(Exception):
    """A close signal whose scope leaves out the level it closes.

    Raised by ``Engine.submit``; see ``PipelineState.check_complete``.
    """


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
        raise ValueError("No workflow found in YAML (need a key other than 'metadata')")

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
                name=name,
                params=raw_params,
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

    Tracks jobs, units, collected results and the job board.
    Thread-safe: all mutable state is protected by _lock.
    """

    def __init__(
        self,
        name,
        yaml_path,
        phases,
        functions_dir,
        step_settings,
        verbose,
        levels=None,
        workflow_name=None,
        output=None,
        registration=None,
    ):
        self.name = name
        self.yaml_path = Path(yaml_path)
        self.phases = phases
        #: The sample's levels, widest first, from the pipeline's metadata.
        #: Without them, the phases' scopes stand in (narrowest first).
        self.levels = list(levels) if levels else None
        self.functions_dir = functions_dir
        self.step_settings = step_settings
        self.verbose = verbose
        #: The pipeline's top-level key, for the results' metadata.
        self.workflow_name = workflow_name or name
        #: Where everything of this pipeline is written: analysis/<name>/.
        self.folder = Path(output) / "analysis" / name
        #: The files handed from step to step, one folder per registration.
        self.handover = self.folder / ".handover" / registration
        self.record = RunRecord(self.folder / "run.jsonl", registration)
        #: The board outputs handed out and still held by someone, by id.
        self.handed = weakref.WeakValueDictionary()

        self._lock = threading.Lock()
        # The job board: id -> {step, scope, state, output[, error]}.
        self._jobs = {}
        self._next_job = 1
        #: Every future this pipeline started, image jobs and scope chains, so
        #: deregister can wait for them or cancel them.
        self.futures = []
        #: Set by deregister(wait=False): no step of this pipeline starts again.
        self.cancelling = False
        # Every scope key a submit has used, for check_complete.
        self._seen_scope_keys = set()

        # Job tracking: [(future, scope_dict, submission_idx)]
        self._job_entries = []

        # Results waiting for the next phase: phase_idx -> [(submission_idx,
        # scope_dict, result)]. For phase 0 the index is the tile's own
        # submit; for a scoped phase it is the submit that closed the unit.
        self._phase_results = defaultdict(list)

        # Scoped phases still running: phase_idx -> [(scope_dict, idx, Event)].
        # A later phase waits on the ones of its unit that were signalled
        # before it (idx <= its own); a signal sent after it is not waited
        # on, or the signal threads could all wait on each other.
        self._scoped_in_flight = defaultdict(list)

        # Failure records for the scoped steps' lineage; scope collection
        # drains them. The board keeps its own record of every failed job.
        self._failures = []

        # The units still open, per scoped level: unit key -> unit.
        self._open = defaultdict(dict)

    def open(self, yaml_text, step_hashes, engine_version):
        """Make the pipeline's folder, keep its YAML, and start the record.

        Hand-over files a crash left behind are cleared first.
        """
        self.folder.mkdir(parents=True, exist_ok=True)
        old = self.handover.parent
        if old.exists():
            for leftover in old.iterdir():
                shutil.rmtree(leftover, ignore_errors=True)
        self.handover.mkdir(parents=True, exist_ok=True)
        (self.folder / "pipeline.yaml").write_text(yaml_text, encoding="utf-8")
        self.record.write(
            "register",
            pipeline=self.name,
            workflow=self.workflow_name,
            yaml=str(self.yaml_path.resolve()),
            yaml_text=yaml_text,
            steps=step_hashes,
            engine=engine_version,
        )

    def close(self):
        """Delete the hand-over files and end the record with a count.

        Every output still held from the board is read first, so it stays
        readable after its files are gone.
        """
        with self._lock:
            held = list(self.handed.values())
        for output in held:
            output.loaded()
        counts = {"done": 0, "failed": 0, "cancelled": 0}
        for entry in self.board().values():
            if entry["state"] in counts:
                counts[entry["state"]] += 1
        shutil.rmtree(self.handover, ignore_errors=True)
        self.record.write("deregister", **counts)

    @property
    def environments(self):
        """The environments this pipeline's steps run in."""
        return {settings["environment"] for settings in self.step_settings.values()}

    def check_complete(self, levels, scope):
        """Refuse a close signal that would close the wrong thing.

        A level that is a scope in this pipeline, left out of a scope that
        earlier submits did name, would match every unit of that level.
        Raises ScopeError. Then remembers this submit's keys. Under the
        caller's lock discipline: called from Engine.submit under its lock,
        and takes this state's lock itself.
        """
        with self._lock:
            for level in levels:
                if (
                    level not in scope
                    and level in self._seen_scope_keys
                    and self.get_triggered_phase_idx(level) is not None
                ):
                    raise ScopeError(
                        f"complete={level!r} with scope {scope!r}: the scope "
                        f"does not name {level!r}, which earlier submits did; "
                        f"this would close every {level} at once"
                    )
            self._seen_scope_keys.update(scope)

    # -- The job board ---------------------------------------------------

    def new_job(self, step, scope, state):
        """Put a new job on the board and return its id."""
        with self._lock:
            job = self._next_job
            self._next_job += 1
            self._jobs[job] = {"step": step, "scope": dict(scope), "state": state, "output": None}
            return job

    def new_image_job(self, scope):
        """An image job, queued at the first step of the pipeline."""
        return self.new_job(self.phases[0].steps[0].name, scope, "queued")

    def new_scope_job(self, phase_idx, unit):
        """A scope job for *unit*, waiting at the first step of its phase."""
        return self.new_job(self.phases[phase_idx].steps[0].name, unit or {}, "waiting")

    def job_at(self, job, step):
        """The job is running *step*."""
        with self._lock:
            self._jobs[job].update(step=step, state="running")

    def job_done(self, job, folder, steps, jobs=None):
        """The job is done; *folder* holds its output. Its other step folders go."""
        self.write_end(job, "done", steps, jobs=jobs)
        with self._lock:
            self._jobs[job].update(state="done", output=folder)
        self.clear_job(job, keep=folder)

    def job_failed(self, job, error, step=None, steps=()):
        """The job failed; a job stopped by deregister is cancelled instead."""
        if self.cancelling:
            self.job_cancelled(job, steps)
            return
        with self._lock:
            entry = self._jobs[job]
            if step:
                entry["step"] = step
        self.write_end(job, "failed", steps, error=error)
        with self._lock:
            entry.update(state="failed", error=error)
        self.clear_job(job)

    def job_cancelled(self, job, steps=()):
        """Cancel the job, unless it already finished."""
        with self._lock:
            if self._jobs[job]["state"] in ("done", "failed", "cancelled"):
                return
            self._jobs[job]["state"] = "cancelled"
        self.write_end(job, "cancelled", steps)
        self.clear_job(job)

    def write_end(self, job, state, steps, error=None, jobs=None):
        """The record's line for a job that ended."""
        with self._lock:
            entry = dict(self._jobs[job])
        self.record.write(
            "end",
            job=job,
            state=state,
            step=entry["step"],
            scope=entry["scope"],
            steps=list(steps),
            error=error,
            jobs=jobs,
        )

    def board(self):
        """A copy of the board. A done job's output is read from its folder
        when it is first looked into."""
        with self._lock:
            entries = {job: dict(entry) for job, entry in self._jobs.items()}
            for entry in entries.values():
                if entry["output"] is not None:
                    entry["output"] = Output(entry["output"])
                    self.handed[id(entry["output"])] = entry["output"]
        return entries

    def final_board(self):
        """The board with every output read now, to keep after the files go."""
        board = self.board()
        for entry in board.values():
            if entry["output"] is not None:
                entry["output"] = read_folder(entry["output"].folder)
        return board

    # -- Hand-over folders ---------------------------------------------------

    def step_folder(self, job, index, step):
        """Where step number *index* of *job* writes its output."""
        return str(self.handover / str(job) / f"{index}-{step}")

    def clear_job(self, job, keep=None):
        """Delete a job's hand-over folders, all but *keep*."""
        folder = self.handover / str(job)
        if not folder.exists():
            return
        for child in folder.iterdir():
            if str(child) != keep:
                shutil.rmtree(child, ignore_errors=True)
        if keep is None:
            shutil.rmtree(folder, ignore_errors=True)

    # -- Futures and failures --------------------------------------------

    def add_job_entry(self, future, scope, submission_idx):
        """Record a submitted image job's future and scope."""
        with self._lock:
            self._job_entries.append((future, scope, submission_idx))
            self.futures.append(future)
        future.add_done_callback(
            lambda done: self.job_cancelled(submission_idx) if done.cancelled() else None
        )

    def add_future(self, future):
        """Record a scope chain's future."""
        with self._lock:
            self.futures.append(future)

    def store_phase0_result(self, submission_idx, scope, result):
        """Store a Phase 0 result for later scope collection."""
        self.store_phase_result(0, result, scope, submission_idx)

    def record_failure(self, scope, step_name, error_msg, phase, submission_idx):
        """Record a failed step for the lineage of the scoped steps above it.
        ``phase`` is the phase it failed in and ``submission_idx`` the tile,
        or the submit that closed the unit."""
        with self._lock:
            self._failures.append(
                {
                    "scope": scope,
                    "step": step_name,
                    "error": error_msg,
                    "phase": phase,
                    "submission_idx": submission_idx,
                }
            )

    def wider_levels(self, level):
        """The levels wider than *level*, widest first: from the pipeline's
        ``levels`` when it has them, else from the later phases' scopes."""
        if self.levels is not None:
            levels = self.levels
            return levels[: levels.index(level)] if level in levels else []
        phases = [phase.scope for phase in self.phases if phase.scope]
        wider = phases[phases.index(level) + 1 :] if level in phases else []
        return wider[::-1]

    def scope_key(self, level, scope):
        """The identity of the unit *level* closes, from a submit's scope.

        The value of every wider level the scope names, widest first, then
        the level's own value. Completing compartment 3 of
        {"carrier": 1, "compartment": 3} is compartment 3 *of carrier 1*.
        ``None`` when the scope does not name the level, which collects
        everything (the "all" case).
        """
        if level not in scope:
            return None
        key = {w: scope[w] for w in self.wider_levels(level) if w in scope}
        key[level] = scope[level]
        return key

    def scoped_levels(self):
        """The levels this pipeline has a step for, narrowest first."""
        return [phase.scope for phase in self.phases if phase.scope]

    def narrower_levels(self, level):
        """The scoped levels narrower than *level*, narrowest first.

        From the pipeline's ``levels`` when *level* is one of them, else from
        the order of the scoped steps. A level known to neither has none.
        """
        scoped = self.scoped_levels()
        if self.levels is not None and level in self.levels:
            below = self.levels[self.levels.index(level) + 1 :]
            return [s for s in scoped if s in below]
        if level in scoped:
            return scoped[: scoped.index(level)]
        return []

    def note_open(self, scope):
        """An image was submitted: every unit it belongs to is open."""
        with self._lock:
            for level in self.scoped_levels():
                unit = self.scope_key(level, scope)
                if unit is not None:
                    self._open[level][tuple(unit.items())] = unit

    def close_signals(self, levels, scope):
        """What a submit's ``complete`` closes, in the order to close it.

        Each level first closes the narrower units of it still open,
        narrowest first, then itself. Returns ``(level, scope)`` pairs: the
        submit's own scope for a level it names, the unit for one it closes
        on the way. A unit is closed once per submit.
        """
        signals, closed = [], set()
        with self._lock:
            for level in levels:
                unit = self.scope_key(level, scope)
                for narrower in self.narrower_levels(level):
                    for key, inner in list(self._open[narrower].items()):
                        if _matches(inner, unit):
                            del self._open[narrower][key]
                            closed.add((narrower, key))
                            signals.append((narrower, dict(inner)))
                key = None if unit is None else tuple(unit.items())
                if key is not None and (level, key) in closed:
                    continue
                if key is not None:
                    self._open[level].pop(key, None)
                    closed.add((level, key))
                signals.append((level, dict(scope)))
        return signals

    def get_triggered_phase_idx(self, level):
        """Find the phase index triggered by a scope level."""
        for i, phase in enumerate(self.phases):
            if phase.scope == level:
                return i
        return None

    def collect_for_scope(self, phase_idx, unit, before):
        """
        Collect the previous phase's results and failures for *unit*, from submits up to and including *before*.

        For Phase 1 (prev=0): collects from Phase 0 results.
        For Phase N (prev=N-1): collects from phase_results[N-1].

        Returns (results, failures) where results is a list sorted by
        submission order and failures is a list of failure records.
        """
        prev_idx = phase_idx - 1

        if prev_idx > 0:
            self._wait_for_scoped(prev_idx, unit, before)

        with self._lock:
            matching, remaining = [], []
            for entry in self._phase_results[prev_idx]:
                idx, entry_scope, result = entry
                if idx <= before and _matches(entry_scope, unit):
                    matching.append((idx, result))
                else:
                    remaining.append(entry)
            self._phase_results[prev_idx] = remaining
            matching.sort(key=lambda x: x[0])
            results = [r for _, r in matching]
            failures = self._take_failures(prev_idx, unit, before)
            return results, failures

    def held_for(self, phase_idx, unit, before):
        """What *unit* still holds from every phase before the one being
        collected: results and failures a narrower level never closed.
        Reported to the step as failures (``step: "engine"``) and kept,
        since that level's signal may still come.
        """
        held = []
        with self._lock:
            for k in range(phase_idx - 1):
                # Phase k's results wait for phase k+1's level to close.
                not_closed = self.phases[k + 1].scope
                for idx, entry_scope, _ in self._phase_results[k]:
                    if idx <= before and _matches(entry_scope, unit):
                        held.append(
                            {
                                "scope": entry_scope,
                                "step": "engine",
                                "error": f"{not_closed} not closed",
                                "phase": k,
                                "submission_idx": idx,
                            }
                        )
                held += [
                    f
                    for f in self._failures
                    if f["phase"] == k
                    and f["submission_idx"] <= before
                    and _matches(f["scope"], unit)
                ]
        held.sort(key=lambda f: f["submission_idx"])
        return held

    def _take_failures(self, phase_idx, unit, before):
        """Remove and return the failures of phase *phase_idx* that belong
        to *unit*, from submits up to *before*. Under _lock."""
        taken, remaining = [], []
        for f in self._failures:
            if (
                f["phase"] == phase_idx
                and f["submission_idx"] <= before
                and _matches(f["scope"], unit)
            ):
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
                self._scoped_in_flight[phase_idx].append((dict(scope), submission_idx, done))
        return phase_idx, done

    def end_scoped(self, token):
        """Mark a scoped phase as finished, whether it succeeded or not."""
        phase_idx, done = token
        if phase_idx is not None:
            with self._lock:
                self._scoped_in_flight[phase_idx] = [
                    entry for entry in self._scoped_in_flight[phase_idx] if entry[2] is not done
                ]
        done.set()

    def _wait_for_scoped(self, phase_idx, unit, before):
        """Wait until every phase *phase_idx* of this unit signalled up to
        *before* is done."""
        with self._lock:
            waiting = [
                done
                for entry_scope, idx, done in self._scoped_in_flight[phase_idx]
                if idx <= before and _matches(entry_scope, unit)
            ]
        for done in waiting:
            done.wait()

    def store_phase_result(self, phase_idx, result, scope, submission_idx):
        """Store a scoped phase result, with its scope and the submit that
        closed it, for the next phase."""
        with self._lock:
            self._phase_results[phase_idx].append((submission_idx, dict(scope), result))

    def get_matching_futures(self, unit, before):
        """Phase 0 futures of *unit*, submitted up to *before*."""
        with self._lock:
            return [
                f for f, scope, idx in self._job_entries if idx <= before and _matches(scope, unit)
            ]

    def cleanup_consumed_entries(self, unit, before):
        """Remove consumed job entries after scope collection."""
        with self._lock:
            self._job_entries = [
                (f, s, idx)
                for f, s, idx in self._job_entries
                if not (idx <= before and _matches(s, unit))
            ]
