"""The engine's integration tests, runnable with plain ``pytest``.

Each test runs one of the pipelines in ``pipelines/`` through the real engine
and checks what the steps reported: that they ran, in which process, and in
which environment. The list of pipelines is shared with ``run_all.py``, so
there is one place to add a case.

The pipelines that stay in the local process need nothing but Python. The
ones that switch environments need conda; when conda is not installed, those
are skipped rather than failed, so the suite gives an honest result on any
machine.

The test environments are created once, before the first test that needs
them. Afterwards the suite removes only the environments it created itself:
one that already existed before the run is left alone. Set
``SMART_KEEP_ENVS=1`` to keep the created ones between runs.

Run from the repository root:

    pytest
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
REPO_ROOT = HERE.parent.parent
ENGINE_DIR = REPO_ROOT / "engine"
for path in (str(ENGINE_DIR), str(HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)

from engine import run_pipeline  # noqa: E402
from run_all import TESTS  # noqa: E402

PREFIX = "SMART--basic_test--"
ENV_A, ENV_B, ENV_C = (PREFIX + name for name in ("env_a", "env_b", "env_c"))

# The pipelines that ask for a conda environment, by name in TESTS.
NEEDS_CONDA = {
    "step_env", "pipeline_env", "combined", "data_survival", "pickle",
    "pipeline_env_pickle", "pipeline_env_json_limit",
}

BY_NAME = {name: (yaml_file, should_pass) for name, yaml_file, should_pass, _ in TESTS}


def _conda_info():
    """conda's own description of itself, or None when conda is not reachable."""
    try:
        from conda_utils import get_conda_info

        return get_conda_info()
    except Exception:
        return None


@pytest.fixture(scope="session")
def test_environments():
    """Create the three test environments once; remove only the ones we created."""
    info = _conda_info()
    if info is None:
        pytest.skip("conda is not installed, so environment switching cannot be tested")

    from conda_utils import env_exists

    wanted = {"env_a": ENV_A, "env_b": ENV_B, "env_c": ENV_C}
    already_there = {step for step, name in wanted.items() if env_exists(info, name)}

    setup = HERE / "environments" / "setup_env.py"
    clean = HERE / "environments" / "clean_env.py"
    result = subprocess.run([sys.executable, str(setup)])
    if result.returncode != 0:
        pytest.fail("could not create the test environments; see the output above")
    yield
    if os.environ.get("SMART_KEEP_ENVS"):
        return
    # Remove one at a time, and only those this run created. An environment
    # that existed before the run belongs to whoever made it, not to us.
    for step in wanted:
        if step not in already_there:
            subprocess.run([sys.executable, str(clean), "--step", step])


def _run(name, request):
    yaml_file, _ = BY_NAME[name]
    if name in NEEDS_CONDA:
        request.getfixturevalue("test_environments")
    return run_pipeline(yaml_path=str(HERE / yaml_file), label=f"test_{name}", input_data={})


# --- pipelines that stay in this process -----------------------------------


def test_local(request):
    result = _run("local", request)
    step = result["step_local"]
    assert step["executed"] is True
    assert step["process_id"] == os.getpid()  # no subprocess for a local step
    assert step["params_used"] == {"test_param": "hello", "number": 42}
    assert result["metadata"]["label"] == "test_local"
    assert result["metadata"]["steps"] == ["step_local"]


def test_mixed(request):
    result = _run("mixed", request)
    first, second = result["step_local"], result["step_local_2"]
    assert first["executed"] and second["executed"]
    assert first["process_id"] == second["process_id"] == os.getpid()
    # The second step saw the first step's output: that is the shared dictionary.
    assert "step_local" in second["previous_steps_found"]
    assert first["params_used"] == {"stage": "first"}
    assert second["params_used"] == {"stage": "second"}


def test_error(request):
    with pytest.raises(RuntimeError, match="Deliberate test error from step_error"):
        _run("error", request)


def test_missing_step(request):
    with pytest.raises(FileNotFoundError):
        _run("missing_step", request)


# --- pipelines that switch environments --------------------------------------


def test_step_env(request):
    result = _run("step_env", request)
    before, switched, after = result["step_local"], result["step_env"], result["step_local_2"]
    assert before["process_id"] == after["process_id"] == os.getpid()
    assert switched["actual_environment"] == ENV_B
    assert switched["environment_match"] is True
    assert switched["process_id"] != os.getpid()  # it really ran elsewhere
    assert switched["params_used"] == {"stage": "in_different_env"}


def test_pipeline_env(request):
    result = _run("pipeline_env", request)
    first, second = result["step_local"], result["step_local_2"]
    # Every step ran in env_b, in one subprocess, not in this process.
    assert first["environment_name"] == second["environment_name"] == ENV_B
    assert first["process_id"] == second["process_id"] != os.getpid()


def test_combined(request):
    result = _run("combined", request)
    outer_1, inner, outer_2 = result["step_local"], result["step_env_b"], result["step_local_2"]
    assert outer_1["environment_name"] == outer_2["environment_name"] == ENV_A
    assert inner["actual_environment"] == ENV_C
    assert inner["environment_match"] is True
    # Three distinct processes: this one, the env_a pipeline, the env_c step.
    assert len({os.getpid(), outer_1["process_id"], inner["process_id"]}) == 3
    assert outer_1["process_id"] == outer_2["process_id"]


def test_data_survival(request):
    result = _run("data_survival", request)
    verify = result["step_verify_data"]
    assert verify["all_passed"] is True, verify["checks"]
    assert result["step_env"]["actual_environment"] == ENV_B
    assert verify["verified_by_pid"] == os.getpid()


def test_pickle(request):
    result = _run("pickle", request)
    pickled = result["step_pickle"]
    assert pickled["data_transfer"] == "pickle"
    assert pickled["environment"] == ENV_B
    assert pickled["received_previous_data"] is True
    assert result["step_verify_data"]["all_passed"] is True


def test_pipeline_env_pickle(request):
    result = _run("pipeline_env_pickle", request)
    step = result["step_bytes"]
    assert step["environment_name"] == ENV_B
    assert step["process_id"] != os.getpid()
    # Bytes cannot travel as JSON; with pickle they come back exactly.
    assert step["payload"] == b"\x00\x01\x02smart"


def test_pipeline_env_json_limit(request):
    # Same step, but the pipeline did not ask for pickle: the engine must say
    # plainly what went wrong and how to fix it, not fail with a bare TypeError.
    with pytest.raises(RuntimeError, match="data_transfer: 'pickle'"):
        _run("pipeline_env_json_limit", request)


# --- the public entry point ---------------------------------------------------


def test_every_pipeline_in_the_shared_list_has_a_test():
    # run_all.py and this file must not drift apart.
    tested = {name[len("test_"):] for name in globals() if name.startswith("test_")}
    assert set(BY_NAME) <= tested, set(BY_NAME) - tested


def test_public_import_works_from_the_repository_root():
    # A fresh Python, started in the repository root with no path tweaks,
    # must be able to import the engine the way the README shows.
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, "-c", "from engine import run_pipeline; print(run_pipeline.__name__)"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "run_pipeline"
