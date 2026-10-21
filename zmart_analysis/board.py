"""
Board -- what a registered pipeline shows while it runs, and what it leaves.

The job board
-------------
Every job has an id, counted per pipeline from 1. A submit makes an image
job; a submit that completes a unit also makes one scope job per level it
closes. Each entry holds the step the job is at, its scope (the unit, for
a scope job), its state and, once done, its output:

    queued -> running -> done | failed          an image job
    waiting -> running -> done | failed         a scope job
    any state before done -> cancelled          deregister(wait=False)

A job is ``queued`` at a step until a worker takes that step, so between
two steps it is queued again at the next one.

A window held until its timepoint before has ended stays ``waiting``,
with ``after`` naming that timepoint.

A job submitted with a deadline also shows ``deadline``, the time by which
the microscope needs it, and once it has ended ``late``: whether it ended
after that time. The engine does not stop a late job; the acquisition
decides what a late answer is worth.

The id doubles as the submission index of units.py: ids only grow, so
"submitted up to and including this one" is still a comparison of numbers.

The run record
--------------
``run.jsonl``: one line of JSON per event (register, submit, a job's end,
deregister). Each line is on disk before the next event, so the record can
be read while the pipeline runs and keeps everything up to a crash.

The hand-over folders
---------------------
One chain's output reaches the next through a folder (handover.py). The
folders of one registration live in ``<registration>/<job>/`` under the
pipeline's hand-over place: ``.handover`` in the pipeline's folder, or,
with ``Engine(handover=...)``, ``zmart-analysis/<user>/<pipeline>`` in the
folder named there (a local disk, away from the acquisition's data). They
are deleted when the pipeline is deregistered; a registration that crashed
leaves them, and the next registration of the pipeline on the same
computer clears them. Only folders named as a registration are cleared:
in a shared folder, anything else stays.
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import shutil
import socket
import threading
import time
import weakref
from datetime import datetime
from pathlib import Path

from .handover import Output, read_folder
from .worker_script import process_alive

logger = logging.getLogger(__name__)

#: The registrations open in this process, by name of their hand-over folder.
LIVE = set()


def leftover(registration):
    """Whether a hand-over folder belongs to no registration that still runs.

    Its name ends in the process and the computer that made it, as
    ``<time>-<pid>@<computer>``: a folder of this process that is no longer
    open, or of a process on this computer that is gone, is left over from
    a crash. Another computer's is left alone, and so is a name that does
    not read as a registration: it is not ours.
    """
    try:
        rest, host = registration.rsplit("@", 1)
        pid = int(rest.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return False
    if host != socket.gethostname():
        return False
    if pid == os.getpid():
        return registration not in LIVE
    return not process_alive(pid)


class RunRecord:
    """Appends events to ``run.jsonl``, each marked with its registration."""

    def __init__(self, path, registration):
        self.path = path
        self.registration = registration
        self.lock = threading.Lock()

    def write(self, event, **fields):
        line = {
            "event": event,
            "registration": self.registration,
            "time": datetime.now().isoformat(timespec="milliseconds"),
            **fields,
        }
        text = json.dumps(line, default=repr) + "\n"
        # A record that cannot be written is reported, and the jobs go on.
        try:
            with self.lock, open(self.path, "a", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
        except OSError as error:
            logger.warning("Run record %s not written: %s", self.path, error)


def described(value):
    """*value* for the record: an array as its shape and type, not its numbers."""
    if hasattr(value, "shape") and hasattr(value, "dtype"):
        return {"array": list(value.shape), "dtype": str(value.dtype)}
    if isinstance(value, dict):
        return {plain_key(key): described(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [described(item) for item in value]
    return value


def plain_key(key):
    """A dictionary key JSON can hold."""
    return key if isinstance(key, (str, int, float, bool)) or key is None else repr(key)


def clock_time(moment):
    """A ``time.time()`` moment as the local clock reads it."""
    return datetime.fromtimestamp(moment).isoformat(timespec="milliseconds")


class Board:
    """The job board, the run record and the hand-over folders of one
    registered pipeline.

    Thread-safe: the board's entries are guarded by ``lock``; the record
    has its own.
    """

    def __init__(self, name, phases, folder, registration, handover=None):
        self.name = name
        self.phases = phases
        #: Where everything of this pipeline is written: analysis/<name>/.
        self.folder = Path(folder)
        #: The files handed from chain to chain, one folder per registration.
        place = (
            Path(handover) / "zmart-analysis" / getpass.getuser() / name
            if handover
            else self.folder / ".handover"
        )
        self.handover = place / registration
        self.record = RunRecord(self.folder / "run.jsonl", registration)
        #: The board outputs handed out and still held by someone, by id.
        self.handed = weakref.WeakValueDictionary()
        #: Set by close(): the hand-over files are gone.
        self.closed = False

        self.lock = threading.Lock()
        #: The board: job id -> {step, scope, state, output[, error]
        #: [, deadline, late]}.
        self.jobs = {}
        self.next_job = 1
        #: The deadlines of the jobs that have one, as ``time.time()`` gives it.
        self.deadlines = {}

    # -- The pipeline's folder ---------------------------------------------

    def open(self, yaml_text, record_fields):
        """Make the pipeline's folder, keep its YAML, and start the record.

        Hand-over files a crash left behind are cleared first; those of a
        registration that still runs are left alone.
        """
        LIVE.add(self.handover.name)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.handover.parent.mkdir(parents=True, exist_ok=True)
        if self.handover.parent.exists():
            for old in self.handover.parent.iterdir():
                if leftover(old.name):
                    shutil.rmtree(old, ignore_errors=True)
        self.handover.mkdir(parents=True, exist_ok=True)
        (self.folder / "pipeline.yaml").write_text(yaml_text, encoding="utf-8")
        self.record.write("register", **record_fields)

    def abandon(self):
        """Undo :meth:`open` for a registration that did not complete."""
        shutil.rmtree(self.handover, ignore_errors=True)
        LIVE.discard(self.handover.name)

    def close(self):
        """Delete the hand-over files and end the record with a count.

        Every output still held from the board is read first, so it stays
        readable after its files are gone. One that cannot be read is left.
        """
        with self.lock:
            self.closed = True
            held = list(self.handed.values())
            states = [entry["state"] for entry in self.jobs.values()]
        for output in held:
            try:
                output.loaded()
            except Exception as error:  # noqa: BLE001 -- an unreadable output stays unread
                logger.warning("Output %s not read: %s", output.folder, error)
        shutil.rmtree(self.handover, ignore_errors=True)
        LIVE.discard(self.handover.name)
        self.record.write(
            "deregister", **{s: states.count(s) for s in ("done", "failed", "cancelled")}
        )

    # -- Jobs --------------------------------------------------------------

    def new_job(self, step, scope, state, deadline_at=None):
        """Put a new job on the board and return its id."""
        with self.lock:
            job = self.next_job
            self.next_job += 1
            self.jobs[job] = {"step": step, "scope": dict(scope), "state": state, "output": None}
            if deadline_at is not None:
                self.deadlines[job] = deadline_at
                self.jobs[job]["deadline"] = clock_time(deadline_at)
            return job

    def new_image_job(self, scope, deadline_at=None):
        """An image job, queued at the first step of the pipeline."""
        return self.new_job(self.phases[0].steps[0].name, scope, "queued", deadline_at)

    def new_scope_job(self, phase_idx, unit, deadline_at=None):
        """A scope job for *unit*, waiting at the first step of its phase."""
        return self.new_job(
            self.phases[phase_idx].steps[0].name, unit or {}, "waiting", deadline_at
        )

    def job_waits(self, job, after):
        """A window waits for its timepoint before, *after*, to end."""
        with self.lock:
            self.jobs[job]["after"] = after

    def job_at(self, job, step):
        """The job is at *step* and waits for a worker to run it."""
        with self.lock:
            self.jobs[job].update(step=step, state="queued")
            self.jobs[job].pop("after", None)

    def job_step(self, job, step):
        """A running job's worker has started *step* (the next of its chain)."""
        with self.lock:
            self.jobs[job]["step"] = step

    def job_running(self, job):
        """A worker has taken the job's step and runs it."""
        with self.lock:
            self.jobs[job]["state"] = "running"

    def job_done(self, job, folder, steps, jobs=None):
        """The job is done; *folder* holds its output. Its other step folders go."""
        self.write_end(job, "done", steps, jobs=jobs)
        with self.lock:
            self.jobs[job].update(state="done", output=folder)
        self.clear_job(job, keep=folder)

    def job_failed(self, job, error, step=None, steps=()):
        """The job failed at *step*."""
        with self.lock:
            entry = self.jobs[job]
            if step:
                entry["step"] = step
        self.write_end(job, "failed", steps, error=error)
        with self.lock:
            entry.update(state="failed", error=error)
        self.clear_job(job)

    def job_cancelled(self, job, steps=()):
        """Cancel the job, unless it already finished."""
        with self.lock:
            if self.jobs[job]["state"] in ("done", "failed", "cancelled"):
                return
            self.jobs[job]["state"] = "cancelled"
        self.write_end(job, "cancelled", steps)
        self.clear_job(job)

    def write_end(self, job, state, steps, error=None, jobs=None):
        """The record's line for a job that ended; a job with a deadline is
        marked late, or not, here."""
        with self.lock:
            if job in self.deadlines:
                self.jobs[job]["late"] = time.time() > self.deadlines[job]
            entry = dict(self.jobs[job])
        timing = {key: entry[key] for key in ("deadline", "late") if key in entry}
        self.record.write(
            "end",
            job=job,
            state=state,
            step=entry["step"],
            scope=entry["scope"],
            steps=list(steps),
            error=error,
            jobs=jobs,
            **timing,
        )

    # -- Reading the board -------------------------------------------------

    def view(self):
        """A copy of the board. A done job's output is read from its folder
        when it is first looked into."""
        with self.lock:
            if self.closed:
                raise KeyError(f"Pipeline '{self.name}' is not registered")
            entries = {job: dict(entry) for job, entry in self.jobs.items()}
            for entry in entries.values():
                if entry["output"] is not None:
                    entry["output"] = Output(entry["output"])
                    self.handed[id(entry["output"])] = entry["output"]
        return entries

    def final(self):
        """The board with every output read now, to keep after the files go.

        An output that cannot be read here is ``None``, with the reason as
        the entry's ``error``.
        """
        with self.lock:
            board = {job: dict(entry) for job, entry in self.jobs.items()}
        read = {}
        for entry in board.values():
            if entry["output"] is not None:
                try:
                    entry["output"] = read_folder(entry["output"], True, read)
                except Exception as error:  # noqa: BLE001 -- reported on the board
                    entry.update(output=None, error=f"output could not be read: {error!r}")
        return board

    # -- Hand-over folders -------------------------------------------------

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
