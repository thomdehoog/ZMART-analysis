"""Tests for ``zmart_analysis.environments``: the setup and cleanup scripts' work.

Nothing here creates a real environment. ``subprocess.run`` is replaced by
a stand-in that records the commands and answers as asked, so the tests
check what the script would do, not whether conda is installed.
"""

from __future__ import annotations

from unittest import mock

import pytest

from zmart_analysis import environments
from zmart_analysis.environments import detect_gpu, get_torch_install_args, gpu_label


def test_detect_gpu_answers_one_of_the_known_backends():
    assert detect_gpu() in {"cpu", "mps", "cu118", "cu121", "cu124", "cu126", "cu128"}


def test_gpu_labels_name_the_backend():
    assert "CPU" in gpu_label("cpu")
    assert "MPS" in gpu_label("mps")
    assert "NVIDIA" in gpu_label("cu124") and "12.4" in gpu_label("cu124")


def test_torch_install_args_point_pip_at_the_right_build():
    assert "--index-url" in get_torch_install_args("cpu")
    assert get_torch_install_args("cpu")[-1].endswith("cpu")
    assert get_torch_install_args("cu124")[-1].endswith("cu124")
    assert "--index-url" not in get_torch_install_args("mps")


class _Ran:
    """What subprocess.run hands back: an exit code and the printed lines."""

    def __init__(self, returncode=0, stdout="OK", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _diagnose(answer):
    with mock.patch.object(environments.subprocess, "run", return_value=answer):
        environments._run_diagnostics(
            "conda", "ZMART--x--main", "python", [("check", "print('x')")]
        )


def test_a_check_that_prints_ok_passes():
    _diagnose(_Ran(stdout="OK"))


def test_a_check_that_prints_fail_stops_the_setup():
    """Python exited normally, but the check said FAIL: that is a failure."""
    with pytest.raises(SystemExit):
        _diagnose(_Ran(stdout="FAIL shift=[-3.  2.]"))


def test_a_check_that_crashes_stops_the_setup():
    with pytest.raises(SystemExit):
        _diagnose(_Ran(returncode=1, stdout="", stderr="ModuleNotFoundError: sklearn"))


def _setup(argv, envs, envs_after=None):
    """Run setup_workflow_env with a pretend conda; return the commands it ran.

    ``envs_after`` is what conda lists once the environment is created.
    """

    def info(listed):
        return {
            "conda_version": "26.1.0",
            "envs": listed,
            "envs_dirs": ["/envs"],
            "root_prefix": "/",
        }

    calls = []

    def conda_info():
        calls.append(1)
        return info(envs if len(calls) == 1 or envs_after is None else envs_after)

    ran = []
    with (
        mock.patch.object(environments, "get_conda_info", side_effect=conda_info),
        mock.patch.object(
            environments.subprocess,
            "run",
            side_effect=lambda cmd, **_: ran.append(cmd) or _Ran(),
        ),
        mock.patch("sys.argv", ["setup_env.py", *argv]),
    ):
        environments.setup_workflow_env(
            workflow="demo",
            pip_packages=["numpy"],
            diagnostics=[("numpy", "import numpy")],
            install_torch=False,
        )
    return ran


def test_check_runs_only_the_diagnostics():
    ran = _setup(["--check"], envs=["/envs/ZMART--demo--main"])
    assert len(ran) == 1
    assert "import numpy" in ran[0]
    assert "create" not in ran[0]


def test_check_of_a_missing_environment_says_so():
    with pytest.raises(SystemExit):
        _setup(["--check"], envs=[])


def test_without_check_an_existing_environment_is_left_alone():
    with pytest.raises(SystemExit):
        _setup([], envs=["/envs/ZMART--demo--main"])


def test_the_packages_and_checks_run_on_the_environments_own_interpreter(tmp_path):
    """Named by its path, not as ``pip`` or ``python``.

    On Windows, ``conda run -n <env> pip`` started base's pip (Python 3.9)
    when base stood first on PATH, and the install failed on a package that
    needs Python 3.12. The engine's workers already name the interpreter by
    its path; the setup does the same.
    """
    prefix = tmp_path / "ZMART--demo--main"
    prefix.mkdir()
    (prefix / "python.exe").write_text("")
    python = str(prefix / "python.exe")

    ran = _setup([], envs=[], envs_after=[str(prefix)])

    install = next(cmd for cmd in ran if "install" in cmd)
    check = next(cmd for cmd in ran if "import numpy" in cmd)
    assert install[install.index(python) :] == [python, "-m", "pip", "install", "numpy"]
    assert check[check.index(python) :] == [python, "-c", "import numpy"]


def test_an_environment_created_without_its_interpreter_stops_the_setup(tmp_path):
    """conda can report "Created" for an environment that holds no python.exe.

    It happened after a run stopped on a full disk: the next create left 12 MB
    behind, and the install failed with "The system cannot find the path
    specified". The setup says what is wrong instead, before installing.
    """
    prefix = tmp_path / "ZMART--demo--main"
    prefix.mkdir()

    with pytest.raises(SystemExit):
        _setup([], envs=[], envs_after=[str(prefix)])


def test_a_dry_run_prints_the_commands_and_runs_none():
    ran = _setup(["--dry-run"], envs=[])
    assert ran == []
