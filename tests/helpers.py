"""What the engine tests build their cases from: step files, recipes, and waiting.

Every engine test writes a small step file and a recipe into one temporary
folder for the whole session, registers the recipe, and waits for the
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


def _wait_for_results(engine, name, expected, timeout=30):
    t0 = time.monotonic()
    collected = []
    while time.monotonic() - t0 < timeout:
        collected.extend(engine.results(name))
        if len(collected) >= expected:
            return collected
        time.sleep(0.05)
    return collected


def _wait_for_status(engine, name, expected_total, timeout=30):
    """Poll engine.status() until completed+failed >= expected_total."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        s = engine.status(name)
        if s["completed"] + s["failed"] >= expected_total:
            return s
        time.sleep(0.05)
    return engine.status(name)
