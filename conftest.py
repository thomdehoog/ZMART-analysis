"""Shared pytest set-up for the ZMART Analysis test suite.

Loaded for every test file in the repository. Puts the package and the
workflows' shared helpers on the path, offers an ``engine_factory`` fixture
that shuts its engines down, the ``wait_for_results`` and ``wait_for_status``
fixtures that poll an engine instead of sleeping, and the Cellpose probe the
object_analysis tests skip on. The step and recipe factories the engine
tests write files with live in ``tests/helpers.py``. The markers are
declared in ``pyproject.toml``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# The package and the workflows' shared helpers are importable wherever
# pytest is started from: the repository root for ``zmart_analysis``, and
# ``workflows/`` for ``shared``.
ROOT = Path(__file__).resolve().parent
for folder in (ROOT, ROOT / "workflows", ROOT / "tests"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

from helpers import _wait_for_results, _wait_for_status  # noqa: E402

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
    from zmart_analysis import Engine

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


@pytest.fixture
def wait_for_results():
    """``wait_for_results(engine, name, expected, timeout=30)``: poll an
    engine until *expected* results of *name* are in, and return them."""
    return _wait_for_results


@pytest.fixture
def wait_for_status():
    """``wait_for_status(engine, name, expected_total, timeout=30)``: poll
    until that many jobs of *name* have completed or failed; return the status."""
    return _wait_for_status


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

    from zmart_analysis.workers import _the_interpreter_in, _the_prefix_of

    prefix = _the_prefix_of(CELLPOSE_ENVIRONMENT)
    if prefix is None:
        _CELLPOSE_RUNTIME_CACHE = (
            False,
            (
                f"the {CELLPOSE_ENVIRONMENT} environment is not set up; create it with "
                "python workflows/object_analysis/environments/setup_env.py --step cellpose"
            ),
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
