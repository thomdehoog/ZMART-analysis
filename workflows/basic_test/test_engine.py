"""The engine's integration tests, runnable with plain ``pytest``.

Each test runs one of the pipelines in ``pipelines/`` through the real engine
and checks that it passes or fails as intended. The list of pipelines is the
same one ``run_all.py`` uses, so there is one place to add a case.

The four pipelines that stay in the local process need nothing but Python.
The five that switch environments need conda; when conda is not installed,
those five are skipped rather than failed, so the suite gives an honest result
on any machine.

The test environments are created once, before the first test that needs them,
and removed again at the end. Set ``SMART_KEEP_ENVS=1`` to keep them between
runs (the setup script skips environments that already exist, so this saves a
few minutes when you run the suite repeatedly).

Run from the repository root:

    pytest
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
ENGINE_DIR = HERE.parent.parent / "engine"
for path in (str(ENGINE_DIR), str(HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)

from engine import run_pipeline  # noqa: E402
from run_all import TESTS  # noqa: E402

# The pipelines that ask for a conda environment, by name in TESTS.
NEEDS_CONDA = {"step_env", "pipeline_env", "combined", "data_survival", "pickle"}


def _conda_available() -> bool:
    """True when the engine can reach conda the way it normally does."""
    try:
        from conda_utils import get_conda_info

        get_conda_info()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def test_environments():
    """Create the three test environments once; remove them afterwards."""
    if not _conda_available():
        pytest.skip("conda is not installed, so environment switching cannot be tested")
    setup = HERE / "environments" / "setup_env.py"
    clean = HERE / "environments" / "clean_env.py"
    result = subprocess.run([sys.executable, str(setup)])
    if result.returncode != 0:
        pytest.fail("could not create the test environments; see the output above")
    yield
    if not os.environ.get("SMART_KEEP_ENVS"):
        subprocess.run([sys.executable, str(clean)])


@pytest.mark.parametrize(
    "name, yaml_file, should_pass",
    [(name, yaml_file, should_pass) for name, yaml_file, should_pass, _ in TESTS],
    ids=[name for name, *_ in TESTS],
)
def test_pipeline(name, yaml_file, should_pass, request):
    if name in NEEDS_CONDA:
        request.getfixturevalue("test_environments")
    yaml_path = HERE / yaml_file
    if should_pass:
        result = run_pipeline(yaml_path=str(yaml_path), label=f"test_{name}", input_data={})
        # Every step that ran leaves its result under its own name.
        assert "metadata" in result and "input" in result
    else:
        with pytest.raises(Exception):
            run_pipeline(yaml_path=str(yaml_path), label=f"test_{name}", input_data={})
