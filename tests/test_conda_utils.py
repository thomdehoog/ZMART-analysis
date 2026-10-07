"""Unit tests for ``engine.conda_utils``.

Run from a conda-enabled terminal::

    pytest tests/test_conda_utils.py -v
"""

import unittest
from pathlib import Path

from engine.conda_utils import (
    detect_gpu,
    env_exists,
    get_conda_exe,
    get_conda_info,
    get_torch_install_args,
    gpu_label,
    list_envs_by_prefix,
)


def _conda_available() -> bool:
    """True when conda can be reached; the tests that need it skip otherwise."""
    try:
        get_conda_info()
        return True
    except Exception:
        return False


needs_conda = unittest.skipUnless(
    _conda_available(), "conda is not installed; skipping the tests that need it"
)


@needs_conda
class TestGetCondaInfo(unittest.TestCase):
    """Tests for get_conda_info()."""

    def test_returns_dict(self):
        info = get_conda_info()
        self.assertIsInstance(info, dict)

    def test_has_required_keys(self):
        info = get_conda_info()
        for key in ["conda_version", "envs", "envs_dirs", "root_prefix"]:
            self.assertIn(key, info, f"Missing key: {key}")

    def test_conda_version_is_string(self):
        info = get_conda_info()
        self.assertIsInstance(info["conda_version"], str)
        # Should look like "X.Y.Z"
        parts = info["conda_version"].split(".")
        self.assertGreaterEqual(len(parts), 2)

    def test_envs_is_list(self):
        info = get_conda_info()
        self.assertIsInstance(info["envs"], list)

    def test_envs_dirs_is_list(self):
        info = get_conda_info()
        self.assertIsInstance(info["envs_dirs"], list)
        self.assertGreater(len(info["envs_dirs"]), 0)

    def test_root_prefix_exists(self):
        info = get_conda_info()
        self.assertTrue(Path(info["root_prefix"]).exists())


@needs_conda
class TestGetCondaExe(unittest.TestCase):
    """Tests for get_conda_exe()."""

    def test_returns_string(self):
        info = get_conda_info()
        exe = get_conda_exe(info)
        self.assertIsInstance(exe, str)

    def test_executable_exists_or_is_conda(self):
        info = get_conda_info()
        exe = get_conda_exe(info)
        # Either a path that exists, or bare "conda" (on PATH)
        self.assertTrue(
            Path(exe).exists() or exe == "conda",
            f"Executable not found: {exe}",
        )


@needs_conda
class TestEnvExists(unittest.TestCase):
    """Tests for env_exists()."""

    def test_existing_env(self):
        info = get_conda_info()
        # At least one env should exist
        if info["envs"]:
            name = Path(info["envs"][0]).name
            self.assertTrue(env_exists(info, name))

    def test_nonexistent_env(self):
        info = get_conda_info()
        self.assertFalse(env_exists(info, "this_env_does_not_exist_12345"))


@needs_conda
class TestListEnvsByPrefix(unittest.TestCase):
    """Tests for list_envs_by_prefix()."""

    def test_returns_list(self):
        info = get_conda_info()
        result = list_envs_by_prefix(info, "SMART--")
        self.assertIsInstance(result, list)

    def test_nonexistent_prefix(self):
        info = get_conda_info()
        result = list_envs_by_prefix(info, "ZZZZZ_NONEXISTENT_PREFIX_")
        self.assertEqual(result, [])

    def test_all_match_prefix(self):
        info = get_conda_info()
        prefix = "SMART--"
        result = list_envs_by_prefix(info, prefix)
        for name in result:
            self.assertTrue(name.startswith(prefix))


class TestDetectGpu(unittest.TestCase):
    """Tests for detect_gpu()."""

    def test_returns_string(self):
        gpu = detect_gpu()
        self.assertIsInstance(gpu, str)

    def test_valid_value(self):
        gpu = detect_gpu()
        valid = {"cpu", "mps", "cu118", "cu121", "cu124", "cu126", "cu128"}
        self.assertIn(gpu, valid, f"Unexpected GPU value: {gpu}")


class TestGpuLabel(unittest.TestCase):
    """Tests for gpu_label()."""

    def test_cpu(self):
        self.assertIn("CPU", gpu_label("cpu"))

    def test_mps(self):
        self.assertIn("MPS", gpu_label("mps"))

    def test_cuda(self):
        label = gpu_label("cu124")
        self.assertIn("NVIDIA", label)
        self.assertIn("12.4", label)


class TestGetTorchInstallArgs(unittest.TestCase):
    """Tests for get_torch_install_args()."""

    def test_cpu_has_index_url(self):
        args = get_torch_install_args("cpu")
        self.assertIn("torch", args)
        self.assertIn("--index-url", args)
        self.assertIn("cpu", args[-1])

    def test_cuda_has_index_url(self):
        args = get_torch_install_args("cu124")
        self.assertIn("torch", args)
        self.assertIn("--index-url", args)
        self.assertIn("cu124", args[-1])

    def test_mps_no_index_url(self):
        args = get_torch_install_args("mps")
        self.assertIn("torch", args)
        self.assertNotIn("--index-url", args)


class _Ran:
    """What subprocess.run hands back: an exit code and the printed lines."""

    def __init__(self, returncode=0, stdout="OK", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class TestDiagnostics(unittest.TestCase):
    """A check that prints FAIL has failed, even though Python exited normally."""

    def _run(self, answer):
        from unittest import mock

        from engine import conda_utils

        with mock.patch.object(conda_utils.subprocess, "run", return_value=answer):
            conda_utils._run_diagnostics("conda", "ZMART--x--main", [("check", "print('x')")])

    def test_ok_passes(self):
        self._run(_Ran(stdout="OK"))

    def test_printed_fail_stops_the_setup(self):
        with self.assertRaises(SystemExit):
            self._run(_Ran(stdout="FAIL shift=[-3.  2.]"))

    def test_a_crash_stops_the_setup(self):
        with self.assertRaises(SystemExit):
            self._run(_Ran(returncode=1, stdout="", stderr="ModuleNotFoundError: sklearn"))


class TestCheckAnExistingEnvironment(unittest.TestCase):
    """``--check`` runs the checks on an environment that is already there."""

    def _setup(self, argv, envs):
        from unittest import mock

        from engine import conda_utils

        info = {"conda_version": "26.1.0", "envs": envs, "envs_dirs": ["/envs"], "root_prefix": "/"}
        ran = []
        with mock.patch.object(conda_utils, "get_conda_info", return_value=info), \
                mock.patch.object(conda_utils.subprocess, "run",
                                  side_effect=lambda cmd, **_: ran.append(cmd) or _Ran()), \
                mock.patch("sys.argv", ["setup_env.py", *argv]):
            conda_utils.setup_workflow_env(
                workflow="demo", pip_packages=["numpy"], diagnostics=[("numpy", "import numpy")],
                install_torch=False,
            )
        return ran

    def test_check_runs_only_the_diagnostics(self):
        ran = self._setup(["--check"], envs=["/envs/ZMART--demo--main"])
        self.assertEqual(len(ran), 1)
        self.assertIn("import numpy", ran[0])
        self.assertNotIn("create", ran[0])

    def test_check_of_a_missing_environment_says_so(self):
        with self.assertRaises(SystemExit):
            self._setup(["--check"], envs=[])

    def test_without_check_an_existing_environment_is_left_alone(self):
        with self.assertRaises(SystemExit):
            self._setup([], envs=["/envs/ZMART--demo--main"])


class TestFindingConda(unittest.TestCase):
    """Conda is found without CONDA_EXE, as in a shell where no env was activated."""

    def test_found_beside_the_running_python(self):
        import os
        import tempfile
        from unittest import mock

        from engine import conda_utils

        with tempfile.TemporaryDirectory() as root:
            env = Path(root) / "envs" / "ZMART--demo--main"
            env.mkdir(parents=True)
            exe = Path(root) / ("Scripts/conda.exe" if os.name == "nt" else "bin/conda")
            exe.parent.mkdir()
            exe.write_text("")
            with mock.patch.dict(os.environ, {}, clear=False), \
                    mock.patch.object(conda_utils.shutil, "which", return_value=None), \
                    mock.patch.object(conda_utils.sys, "prefix", str(env)):
                os.environ.pop("CONDA_EXE", None)
                self.assertEqual(conda_utils._find_conda(), str(exe))


if __name__ == "__main__":
    unittest.main()
