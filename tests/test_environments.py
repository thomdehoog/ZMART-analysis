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
        environments._run_diagnostics("conda", "ZMART--x--main", [("check", "print('x')")])


def test_a_check_that_prints_ok_passes():
    _diagnose(_Ran(stdout="OK"))


def test_a_check_that_prints_fail_stops_the_setup():
    """Python exited normally, but the check said FAIL: that is a failure."""
    with pytest.raises(SystemExit):
        _diagnose(_Ran(stdout="FAIL shift=[-3.  2.]"))


def test_a_check_that_crashes_stops_the_setup():
    with pytest.raises(SystemExit):
        _diagnose(_Ran(returncode=1, stdout="", stderr="ModuleNotFoundError: sklearn"))


def _setup(argv, envs):
    """Run setup_workflow_env with a pretend conda; return the commands it ran."""
    info = {"conda_version": "26.1.0", "envs": envs, "envs_dirs": ["/envs"], "root_prefix": "/"}
    ran = []
    with (
        mock.patch.object(environments, "get_conda_info", return_value=info),
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
