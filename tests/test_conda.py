"""Tests for ``zmart_analysis.conda``: finding conda and its environments.

The tests that ask a real conda skip when there is none, so the suite
runs the same in a plain Python and in a conda-enabled terminal.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from zmart_analysis import conda
from zmart_analysis.conda import env_exists, get_conda_exe, get_conda_info, list_envs_by_prefix


def _conda_available() -> bool:
    """True when conda can be reached; the tests that need it skip otherwise."""
    try:
        get_conda_info()
        return True
    except Exception:
        return False


needs_conda = pytest.mark.skipif(
    not _conda_available(), reason="conda is not installed; skipping the tests that need it"
)


@needs_conda
class TestGetCondaInfo:
    def test_has_the_keys_the_engine_reads(self):
        info = get_conda_info()
        assert isinstance(info, dict)
        for key in ("conda_version", "envs", "envs_dirs", "root_prefix"):
            assert key in info, f"Missing key: {key}"

    def test_the_version_looks_like_one(self):
        info = get_conda_info()
        assert isinstance(info["conda_version"], str)
        assert len(info["conda_version"].split(".")) >= 2

    def test_the_environment_lists_are_lists(self):
        info = get_conda_info()
        assert isinstance(info["envs"], list)
        assert isinstance(info["envs_dirs"], list) and info["envs_dirs"]
        assert Path(info["root_prefix"]).exists()


@needs_conda
class TestGetCondaExe:
    def test_an_executable_that_exists_or_the_bare_name(self):
        exe = get_conda_exe(get_conda_info())
        assert isinstance(exe, str)
        assert Path(exe).exists() or exe == "conda", f"Executable not found: {exe}"


@needs_conda
class TestEnvExists:
    def test_an_existing_environment_is_found(self):
        info = get_conda_info()
        if info["envs"]:
            assert env_exists(info, Path(info["envs"][0]).name)

    def test_a_missing_environment_is_not(self):
        assert not env_exists(get_conda_info(), "this_env_does_not_exist_12345")


@needs_conda
class TestListEnvsByPrefix:
    def test_only_names_with_the_prefix_come_back(self):
        info = get_conda_info()
        assert list_envs_by_prefix(info, "ZZZZZ_NONEXISTENT_PREFIX_") == []
        for name in list_envs_by_prefix(info, "ZMART--"):
            assert name.startswith("ZMART--")


def test_env_exists_and_list_envs_read_the_names_from_the_paths():
    """No conda needed: the two helpers only look at the paths conda lists."""
    info = {"envs": ["/opt/conda", "/opt/conda/envs/ZMART--focus--main", "/x/envs/other"]}
    assert env_exists(info, "ZMART--focus--main")
    assert not env_exists(info, "ZMART--focus")
    assert list_envs_by_prefix(info, "ZMART--") == ["ZMART--focus--main"]


def test_conda_is_found_beside_the_running_python(tmp_path):
    """Without CONDA_EXE and without conda on PATH, as in a shell where no
    environment was activated, conda is looked for in the installation the
    running Python belongs to."""
    env = tmp_path / "envs" / "ZMART--demo--main"
    env.mkdir(parents=True)
    exe = tmp_path / ("Scripts/conda.exe" if os.name == "nt" else "bin/conda")
    exe.parent.mkdir()
    exe.write_text("")
    with (
        mock.patch.dict(os.environ, {}, clear=False),
        mock.patch.object(conda.shutil, "which", return_value=None),
        mock.patch.object(conda.sys, "prefix", str(env)),
    ):
        os.environ.pop("CONDA_EXE", None)
        assert conda._find_conda() == str(exe)
