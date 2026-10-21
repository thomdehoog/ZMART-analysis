"""
Units -- which images belong together, and when a group of them is complete.

An image names the units it belongs to in its scope, widest first, for
example {"carrier": 1, "compartment": 3}. A scoped phase of the pipeline
runs once per unit of its level, when the acquisition says that unit is
complete. This module keeps that bookkeeping for one registered pipeline:
which units are open, what a close signal closes, which results and
failures belong to a unit, and which closes are still running.

Scope matching
--------------
When complete="X" is signalled from a submit with scope={"X": val}:
  - If "X" is a key in the scope dicts: match by value (scope["X"] == val),
    together with the value of every wider level (see scope_key). The
    wider levels are the pipeline's ``levels`` when it declares them, else
    the scopes of the later phases.
  - If "X" is not a key: collect everything from the previous phase.

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

Time
----
An image may name its ``timepoint``, a whole number the acquisition gives;
the engine never counts. A unit is then its place at that timepoint: the
key gets the timepoint too, so compartment 3 at timepoint 4 and at
timepoint 5 stay apart. A step over everything is one unit per timepoint.
``complete="timepoint"`` closes the round: every open unit of that
timepoint, narrowest first. ``complete`` of a level the images do not name
(the experiment) still closes everything open, each unit at its own
timepoint. A position closes itself when its image is done, so it is never
open and never closed by a signal.

A phase over N timepoints (a window, ``Windows`` below) sees its place at
this timepoint and the ones before it within N, and runs in timepoint order
per place.

Order and identity
------------------
What is kept for the next phase is a ``Stored``: the folder of the output,
the job that made it and the scope it is from (an image's scope, or the
unit a scope job closed), and the summary its lineage is built from. The
order it is collected in is the submit it came with: for a unit, the
submit that closed it. That is not the scope job's id, which a submit hands
out after its image's, so a carrier closed by the same submit as its last
compartment still collects that compartment.

Up the levels
-------------
A scoped step receives the previous phase's failures of its unit, and what
a narrower level never closed, as failures too, each as
``{"id", "scope", "step", "error"}``. ``ScopeError`` is raised by a close
signal whose scope leaves out the level it closes.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import NamedTuple


class ScopeError(Exception):
    """A close signal whose scope leaves out the level it closes.

    Raised by ``Engine.submit``; see ``Units.check_complete``.
    """


class Stored(NamedTuple):
    """A phase's output, kept for the next phase."""

    folder: str
    #: The job that made it, as on the board.
    id: int
    #: Where it is from: an image's scope, or the unit a scope job closed.
    scope: dict
    #: What its lineage is built from (handover.summary_of).
    summary: dict


def given(failure):
    """A failure as a scoped step receives it: which job, where, which step, why."""
    return {key: failure[key] for key in ("id", "scope", "step", "error")}


def matches(entry_scope, key):
    """Whether a job's scope belongs to the unit *key* names.

    *key* is a dict such as {"compartment": 3, "carrier": 1}: the level being
    completed and every wider level. All of them must agree, because
    compartment 3 exists on every carrier. ``None`` matches everything.
    """
    if key is None:
        return True
    return all(entry_scope.get(k) == v for k, v in key.items())


class Units:
    """The units of one registered pipeline: open, closed, and what they hold.

    Thread-safe: every mutable field is guarded by ``lock``.
    """

    def __init__(self, phases, levels=None):
        self.phases = phases
        #: The sample's levels, widest first, from the pipeline's metadata.
        #: Without them, the phases' scopes stand in (narrowest first).
        self.levels = list(levels) if levels else None

        self.lock = threading.Lock()
        #: Every scope key a submit has used, for check_complete.
        self.seen_scope_keys = set()
        #: The image jobs: [(future, scope, submission_idx)].
        self.entries = []
        #: Results waiting for the next phase: phase_idx -> [(submission_idx,
        #: scope, Stored)]. For phase 0 the index and scope are the image's
        #: own; for a scoped phase they are those of the submit that closed
        #: the unit (see "Order and identity").
        self.phase_results = defaultdict(list)
        #: Scoped phases still running: phase_idx -> [(scope, idx, Event)].
        #: A later phase waits on the ones of its unit that were signalled
        #: before it (idx <= its own); a signal sent after it is not waited
        #: on, or the signal threads could all wait on each other.
        self.in_flight = defaultdict(list)
        #: Failure records for the scoped steps' lineage; collecting a unit
        #: drains them. The board keeps its own record of every failed job.
        self.failures = []
        #: The units still open, per scoped level: unit key -> unit.
        self.open = defaultdict(dict)
        #: The phases over several timepoints, and what they keep.
        self.windows = Windows(phases)

    # -- Levels ----------------------------------------------------------

    def phase_for(self, level):
        """The index of the phase *level* triggers, or None."""
        for i, phase in enumerate(self.phases):
            if phase.scope == level:
                return i
        return None

    def scoped_levels(self):
        """The levels this pipeline closes, narrowest first: every scoped
        level but ``position``, which closes itself with its image."""
        return [phase.scope for phase in self.phases if phase.scope not in (None, "position")]

    def wider_levels(self, level):
        """The levels wider than *level*, widest first: from the pipeline's
        ``levels`` when it has them, else from the later phases' scopes."""
        if self.levels is not None:
            return self.levels[: self.levels.index(level)] if level in self.levels else []
        scoped = self.scoped_levels()
        wider = scoped[scoped.index(level) + 1 :] if level in scoped else []
        return wider[::-1]

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

    def scope_key(self, level, scope):
        """The identity of the unit *level* closes, from a submit's scope.

        The value of every wider level the scope names, widest first, then
        the level's own value. Completing compartment 3 of
        {"carrier": 1, "compartment": 3} is compartment 3 *of carrier 1*.
        ``None`` when the scope does not name the level, which collects
        everything (the "all" case). A scope with a ``timepoint`` adds it:
        the unit is its place at that timepoint, and everything is one unit
        per timepoint.
        """
        if level in scope:
            key = {w: scope[w] for w in self.wider_levels(level) if w in scope}
            key[level] = scope[level]
        else:
            key = None
        if "timepoint" in scope:
            key = {**(key or {}), "timepoint": scope["timepoint"]}
        return key

    # -- Submits and close signals ---------------------------------------

    def check_complete(self, levels, scope):
        """Refuse a close signal that would close the wrong thing.

        A level that is a scope in this pipeline, left out of a scope that
        earlier submits did name, would match every unit of that level.
        ``complete="timepoint"`` needs the timepoint it closes, and a
        pipeline that looks over timepoints needs every image's timepoint.
        Raises ScopeError. Then remembers this submit's keys.
        """
        if "timepoint" not in scope and ("timepoint" in levels or self.windows.phases):
            raise ScopeError(
                f"scope {scope!r} names no 'timepoint', which "
                + (
                    "complete='timepoint' closes"
                    if "timepoint" in levels
                    else "this pipeline's windows need"
                )
            )
        with self.lock:
            for level in levels:
                if (
                    level not in scope
                    and level in self.seen_scope_keys
                    and self.phase_for(level) is not None
                ):
                    raise ScopeError(
                        f"complete={level!r} with scope {scope!r}: the scope "
                        f"does not name {level!r}, which earlier submits did; "
                        f"this would close every {level} at once"
                    )
            self.seen_scope_keys.update(scope)

    def note_open(self, scope):
        """An image was submitted: every unit it belongs to is open, and its
        timepoint is one its place has, for the windows."""
        with self.lock:
            for level in self.scoped_levels():
                unit = self.scope_key(level, scope)
                if unit is not None:
                    self.open[level][tuple(unit.items())] = unit
        for phase_idx, phase in self.windows.phases.items():
            unit = self.scope_key(phase.scope, scope)
            if unit is not None and phase.scope in unit:
                self.windows.opened(phase_idx, unit)

    def close_signals(self, levels, scope):
        """What a submit's ``complete`` closes, in the order to close it.

        Each level first closes the narrower units of it still open,
        narrowest first, then itself. Returns ``(level, scope)`` pairs: the
        submit's own scope for a level it names, the unit for one it closes
        on the way. A unit is closed once per submit.
        """
        signals, closed = [], set()

        def close_open(level, unit):
            for key, inner in list(self.open[level].items()):
                if matches(inner, unit):
                    del self.open[level][key]
                    closed.add((level, key))
                    signals.append((level, dict(inner)))

        with self.lock:
            for level in levels:
                if level == "timepoint":
                    # The round: every open unit of this timepoint.
                    for narrower in self.scoped_levels():
                        close_open(narrower, {"timepoint": scope["timepoint"]})
                    continue
                # A level the scope does not name is everything, at every
                # timepoint; one it names is its unit at this timepoint.
                unit = self.scope_key(level, scope) if level in scope else None
                for narrower in self.narrower_levels(level):
                    close_open(narrower, unit)
                if level not in scope and self.open[level]:
                    # Everything, kept per timepoint: each its own close.
                    close_open(level, None)
                    continue
                key = None if unit is None else tuple(unit.items())
                if key is not None and (level, key) in closed:
                    continue
                if key is not None:
                    self.open[level].pop(key, None)
                    closed.add((level, key))
                signals.append((level, dict(scope)))
        return signals

    def add_entry(self, future, scope, submission_idx):
        """Remember an image job's future, for the closes of its units."""
        with self.lock:
            self.entries.append((future, scope, submission_idx))

    def futures_of(self, unit, before):
        """The image jobs' futures of *unit*, submitted up to *before*."""
        with self.lock:
            return [f for f, scope, idx in self.entries if idx <= before and matches(scope, unit)]

    def forget_entries(self, unit, before):
        """Drop the image jobs of *unit* up to *before*: their unit is collected."""
        with self.lock:
            self.entries = [
                (f, s, idx)
                for f, s, idx in self.entries
                if not (idx <= before and matches(s, unit))
            ]

    # -- Results and failures --------------------------------------------

    def store_result(self, phase_idx, stored, scope, submission_idx):
        """Keep a phase's output for the next phase, under the scope and the
        submit it is collected by."""
        with self.lock:
            self.phase_results[phase_idx].append((submission_idx, dict(scope), stored))

    def record_failure(self, failed, step_name, error_msg, phase, scope, submission_idx):
        """Record a failed job for the scoped steps above it.

        *failed* is the job as on the board, ``(id, scope)``: an image, or a
        scope job and its unit. *phase* is the phase it failed in; *scope*
        and *submission_idx* are what it is collected by, as for a result.
        """
        job, failed_scope = failed
        with self.lock:
            self.failures.append(
                {
                    "id": job,
                    "scope": dict(failed_scope),
                    "step": step_name,
                    "error": error_msg,
                    "phase": phase,
                    "match": dict(scope),
                    "submission_idx": submission_idx,
                }
            )

    def collect(self, phase_idx, unit, before):
        """The previous phase's outputs and failures for *unit*, from submits
        up to and including *before*, taken out of the store.

        Waits first for the previous phase's runs of this unit, when that
        phase is scoped. Returns (stored, failures): the ``Stored`` outputs
        in submission order, and the failures as a step receives them.
        """
        prev_idx = phase_idx - 1

        if prev_idx > 0:
            self.wait_for_scoped(prev_idx, unit, before)

        with self.lock:
            matching, remaining = [], []
            for entry in self.phase_results[prev_idx]:
                idx, entry_scope, result = entry
                if idx <= before and matches(entry_scope, unit):
                    matching.append((idx, result))
                else:
                    remaining.append(entry)
            self.phase_results[prev_idx] = remaining
            matching.sort(key=lambda x: x[0])
            stored = [item for _, item in matching]
            failures = [given(f) for f in self.take_failures(prev_idx, unit, before)]
            return stored, failures

    def held_for(self, phase_idx, unit, before):
        """What *unit* still holds from every phase before the one being
        collected: results and failures a narrower level never closed.
        Reported to the step as failures (``step: "engine"``) and kept,
        since that level's signal may still come.
        """
        held = []
        with self.lock:
            for k in range(phase_idx - 1):
                # Phase k's results wait for phase k+1's level to close.
                not_closed = self.phases[k + 1].scope
                for idx, entry_scope, stored in self.phase_results[k]:
                    if idx <= before and matches(entry_scope, unit):
                        note = {
                            "id": stored.id,
                            "scope": stored.scope,
                            "step": "engine",
                            "error": f"{not_closed} not closed",
                        }
                        held.append((idx, note))
                held += [
                    (f["submission_idx"], given(f))
                    for f in self.failures
                    if f["phase"] == k
                    and f["submission_idx"] <= before
                    and matches(f["match"], unit)
                ]
        held.sort(key=lambda pair: pair[0])
        return [note for _, note in held]

    def take_failures(self, phase_idx, unit, before):
        """Remove and return the failures of phase *phase_idx* that belong
        to *unit*, from submits up to *before*. Called under ``lock``."""
        taken, remaining = [], []
        for f in self.failures:
            if (
                f["phase"] == phase_idx
                and f["submission_idx"] <= before
                and matches(f["match"], unit)
            ):
                taken.append(f)
            else:
                remaining.append(f)
        self.failures = remaining
        return taken

    # -- Scoped phases in flight -----------------------------------------

    def begin_scoped(self, phase_idx, scope, submission_idx):
        """Mark a scoped phase as running for *scope*; returns its token.

        A level with no phase in this pipeline gets a token that marks
        nothing, so the caller can treat every level alike.
        """
        done = threading.Event()
        if phase_idx is not None:
            with self.lock:
                self.in_flight[phase_idx].append((dict(scope), submission_idx, done))
        return phase_idx, done

    def end_scoped(self, token):
        """Mark a scoped phase as finished, whether it succeeded or not."""
        phase_idx, done = token
        if phase_idx is not None:
            with self.lock:
                self.in_flight[phase_idx] = [
                    entry for entry in self.in_flight[phase_idx] if entry[2] is not done
                ]
        done.set()

    def wait_for_scoped(self, phase_idx, unit, before):
        """Wait until every run of phase *phase_idx* of this unit, signalled
        up to *before*, is done."""
        with self.lock:
            waiting = [
                done
                for entry_scope, idx, done in self.in_flight[phase_idx]
                if idx <= before and matches(entry_scope, unit)
            ]
        for done in waiting:
            done.wait()


class Windows:
    """The phases that look at several timepoints of their place.

    A window phase at timepoint t sees its place's parts of the timepoints it
    has within ``t - N + 1 ... t`` (a skipped round is missing, not empty),
    and its own output at the timepoint before: the latest earlier timepoint
    the place has. The runs of one place go in timepoint order: a run whose
    timepoint before has not finished is held, and the run that finishes it
    starts it. No thread waits for another.

    What a window no longer needs is let go: the parts of a place older than
    the next window can see, and every output but the last.
    """

    def __init__(self, phases):
        #: The window phases, by index.
        self.phases = {i: phase for i, phase in enumerate(phases) if phase.window}
        self.lock = threading.Lock()
        #: (phase_idx, place) -> the timepoints the place has.
        self.timepoints = defaultdict(set)
        #: (phase_idx, place) -> {timepoint: [Stored]}, the parts kept.
        self.parts = defaultdict(dict)
        #: (phase_idx, place) -> {timepoint: folder or None}: the last output.
        self.outputs = defaultdict(dict)
        #: (phase_idx, place) -> the timepoints whose run has ended.
        self.ended = defaultdict(set)
        #: (phase_idx, place) -> {timepoint: (job, run)}, the runs held.
        self.held = defaultdict(dict)

    @staticmethod
    def place_of(phase_idx, unit):
        """The window a unit belongs to, and its timepoint."""
        place = tuple((k, v) for k, v in unit.items() if k != "timepoint")
        return (phase_idx, place), unit["timepoint"]

    def opened(self, phase_idx, unit):
        """The place of *unit* has its timepoint."""
        window, t = self.place_of(phase_idx, unit)
        with self.lock:
            self.timepoints[window].add(t)

    def before(self, window, t):
        """The latest timepoint of the window earlier than *t*, or None."""
        earlier = [tp for tp in self.timepoints[window] if tp < t]
        return max(earlier) if earlier else None

    def turn(self, phase_idx, unit, stored, job, run):
        """Keep the parts of *unit*; whether its run may start now.

        When its timepoint before has not finished yet, *run* is held and
        started by the run that finishes it. Returns the timepoint it waits
        for, or None when it may start now.
        """
        window, t = self.place_of(phase_idx, unit)
        with self.lock:
            self.parts[window][t] = list(stored)
            earlier = self.before(window, t)
            if earlier is None or earlier in self.ended[window]:
                return None
            self.held[window][t] = (job, run)
            return earlier

    def given(self, phase_idx, unit):
        """What the run of *unit* sees: its parts by timepoint, oldest first,
        and the output folder of its timepoint before (None without one)."""
        window, t = self.place_of(phase_idx, unit)
        n = self.phases[phase_idx].timepoints
        with self.lock:
            groups = [
                (tp, self.parts[window][tp])
                for tp in sorted(self.parts[window])
                if t - n + 1 <= tp <= t
            ]
            earlier = self.before(window, t)
            previous = None if earlier is None else self.outputs[window].get(earlier)
        return groups, previous

    def finished(self, phase_idx, unit, folder):
        """The run of *unit* ended with *folder* (None if it failed).

        Lets go of what no later window needs, and returns the held run that
        was waiting for this one, or None.
        """
        window, t = self.place_of(phase_idx, unit)
        n = self.phases[phase_idx].timepoints
        with self.lock:
            self.ended[window].add(t)
            self.outputs[window] = {t: folder}
            self.parts[window] = {
                tp: stored for tp, stored in self.parts[window].items() if tp >= t - n + 2
            }
            for later, (_, run) in sorted(self.held[window].items()):
                if self.before(window, later) == t:
                    del self.held[window][later]
                    return run
        return None

    def kept(self):
        """How many timepoints of parts are kept, over every place."""
        with self.lock:
            return sum(len(parts) for parts in self.parts.values())

    def still_held(self):
        """The jobs of runs still held: their timepoint before never ended."""
        with self.lock:
            return [job for held in self.held.values() for job, _ in held.values()]
