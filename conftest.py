"""Shared pytest helpers for the ZMART Analysis test suite.

Loaded for every test file in the repository. Provides a session-wide temp
directory, factories for step files and recipes (``_write_step``,
``_write_yaml``), polling helpers that wait on engine state instead of
sleeping, an ``engine_factory`` fixture that shuts its engines down, and the
Cellpose probe the object_analysis tests skip on. The markers are declared
in ``pyproject.toml``.
"""

from __future__ import annotations

import atexit
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest


# Make the engine package importable regardless of where pytest is invoked
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


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


# --------------------------------------------------------------------------
# Engine fixture
# --------------------------------------------------------------------------


@pytest.fixture
def engine_factory():
    """Returns a callable that builds an Engine and ensures shutdown.

    Usage::

        def test_something(engine_factory):
            with engine_factory() as e:
                e.register("test", yaml_path)
                ...

    The factory accepts the same kwargs as ``engine.Engine``.
    """
    from engine import Engine

    created = []

    def _make(**kwargs):
        e = Engine(**kwargs)
        created.append(e)
        return e

    yield _make

    for e in created:
        try:
            e.shutdown()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Skip helpers
# --------------------------------------------------------------------------


#: The environment the Cellpose step runs in (``environment`` in
#: ``detect_objects.py``). The tests run Cellpose through the engine, so the
#: probe looks there, not in the Python that runs pytest.
CELLPOSE_ENVIRONMENT = "ZMART--object_analysis--cellpose"

_CELLPOSE_RUNTIME_CACHE: tuple[bool, str] | None = None


def _has_cellpose_runtime() -> tuple[bool, str]:
    """Return (available, detail) for Cellpose in its own environment.

    When available, ``detail`` is the device that environment's torch sees,
    ``"cuda"`` or ``"cpu"``, so a test can check that a GPU which is there
    was really used. Otherwise it says why, in a way that tells the reader
    what to set up.

    The probe runs in a subprocess: broken Windows torch installs can fail
    while loading native DLLs, which is not always cleanly contained by
    catching Python exceptions in the pytest process.
    """
    global _CELLPOSE_RUNTIME_CACHE
    if _CELLPOSE_RUNTIME_CACHE is not None:
        return _CELLPOSE_RUNTIME_CACHE

    from engine.workers import _the_interpreter_in, _the_prefix_of

    prefix = _the_prefix_of(CELLPOSE_ENVIRONMENT)
    if prefix is None:
        _CELLPOSE_RUNTIME_CACHE = False, (
            f"the {CELLPOSE_ENVIRONMENT} environment is not set up; create it with "
            "python workflows/object_analysis/environments/setup_env.py --step cellpose"
        )
        return _CELLPOSE_RUNTIME_CACHE

    code = r"""
import importlib
import sys

for module in ("skimage", "cellpose.models", "torch"):
    try:
        importlib.import_module(module)
    except Exception as exc:
        print(
            f"{module} unavailable: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(2)
import torch
print("cuda" if torch.cuda.is_available() else "cpu")
"""
    try:
        result = subprocess.run(
            [str(_the_interpreter_in(prefix)), "-c", code],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        _CELLPOSE_RUNTIME_CACHE = False, "runtime probe timed out"
        return _CELLPOSE_RUNTIME_CACHE

    if result.returncode == 0:
        _CELLPOSE_RUNTIME_CACHE = True, result.stdout.strip().splitlines()[-1]
        return _CELLPOSE_RUNTIME_CACHE

    detail = result.stderr.strip() or result.stdout.strip()
    if not detail:
        detail = f"runtime probe exited with code {result.returncode}"
    _CELLPOSE_RUNTIME_CACHE = False, detail
    return _CELLPOSE_RUNTIME_CACHE




@pytest.fixture
def cellpose_device():
    """The device Cellpose's environment can use, ``"cuda"`` or ``"cpu"``."""
    available, detail = _has_cellpose_runtime()
    if not available:
        pytest.skip(f"cellpose unavailable: {detail}")
    return detail


def pytest_collection_modifyitems(config, items):
    """Auto-skip tests whose optional runtimes are unavailable."""
    if any("cellpose" in item.keywords for item in items):
        available, reason = _has_cellpose_runtime()
        if not available:
            skip = pytest.mark.skip(reason=f"cellpose unavailable: {reason}")
            for item in items:
                if "cellpose" in item.keywords:
                    item.add_marker(skip)

