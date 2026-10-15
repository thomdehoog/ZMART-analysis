"""What the engine tests build their cases from: step files, pipelines, and waiting.

Every engine test writes a small step file and a pipeline into one temporary
folder for the whole session, registers the pipeline, and waits for the
engine to finish. These are the few functions that do that, kept in a
module of their own so a test can import them by name (``conftest.py`` is
pytest's and cannot be imported from a test under two folders at once).
"""

from __future__ import annotations

import atexit
import shutil
import tempfile
import textwrap
import time
from pathlib import Path

# --------------------------------------------------------------------------
# Session-wide temp dir
# --------------------------------------------------------------------------

_SESSION_TEMP = Path(tempfile.mkdtemp(prefix="zmart_analysis_tests_"))
atexit.register(shutil.rmtree, _SESSION_TEMP, True)
_counter = [0]


def _next_id():
    _counter[0] += 1
    return _counter[0]


# --------------------------------------------------------------------------
# Step / YAML factories
# --------------------------------------------------------------------------


def _write_step(code, name=None):
    """Write a step .py file to the session temp dir; return absolute path."""
    fname = f"{name}.py" if name else f"step_{_next_id()}.py"
    path = _SESSION_TEMP / fname
    path.write_text(textwrap.dedent(code))
    return str(path)


def _write_yaml(content):
    """Write a pipeline YAML to the session temp dir; return absolute path.

    If the content does not declare ``functions_dir``, one pointing at the
    session temp dir is injected so steps written by ``_write_step`` resolve.
    """
    text = textwrap.dedent(content)
    if "functions_dir" not in text:
        functions_dir = _SESSION_TEMP.as_posix()
        header = f'metadata:\n  functions_dir: "{functions_dir}"\n'
        if "metadata:" in text:
            text = text.replace("metadata:", header.rstrip("\n"), 1)
        else:
            text = header + text
    path = _SESSION_TEMP / f"pipeline_{_next_id()}.yaml"
    path.write_text(text)
    return str(path)


# --------------------------------------------------------------------------
# Polling helpers
# --------------------------------------------------------------------------


FINISHED = ("done", "failed", "cancelled")


def outputs(board):
    """The outputs of the done jobs on a board, in job order."""
    return [board[job]["output"] for job in sorted(board) if board[job]["state"] == "done"]


def phase_of(output):
    """0 for an image job's output, 1, 2, ... for a scoped one."""
    return output["metadata"].get("phase", 0)


def tally(board):
    """How many jobs of a board are in each state."""
    counts = dict.fromkeys(("queued", "running", "waiting", "done", "failed", "cancelled"), 0)
    for entry in board.values():
        counts[entry["state"]] += 1
    return counts


def failures(board):
    """The failed entries of a board, in job order, each with its job id."""
    return [{"job": job, **board[job]} for job in sorted(board) if board[job]["state"] == "failed"]


def _wait_for_results(engine, name, expected, timeout=30):
    """Poll the board until *expected* jobs are done; return their outputs."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        done = outputs(engine.status(name))
        if len(done) >= expected:
            return done
        time.sleep(0.05)
    return outputs(engine.status(name))


def _wait_for_status(engine, name, expected_total, timeout=30):
    """Poll the board until *expected_total* jobs have finished; return it."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        board = engine.status(name)
        if sum(entry["state"] in FINISHED for entry in board.values()) >= expected_total:
            return board
        time.sleep(0.05)
    return engine.status(name)
