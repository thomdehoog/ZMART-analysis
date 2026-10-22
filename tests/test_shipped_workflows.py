"""The package is the engine: an install brings no workflows.

A pipeline, its steps and its lock files live with the workflow that uses
them; the workflows in this repository are examples to copy, not installed.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _clean_copy(target: Path) -> Path:
    """The tracked files only, as a clone has them: no build/ left over from an earlier build."""
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO, check=True, capture_output=True, text=True
    ).stdout.splitlines()
    for name in tracked:
        source = REPO / name
        if source.is_file():
            (target / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target / name)
    return target


@pytest.mark.slow
def test_the_wheel_is_the_engine_only(tmp_path):
    source = _clean_copy(tmp_path / "source")
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", str(source), "--no-deps", "--no-build-isolation",
         "-w", str(tmp_path / "wheel"), "-q"],
        check=True,
    )
    wheel = next((tmp_path / "wheel").glob("zmart_analysis-*.whl"))
    names = zipfile.ZipFile(wheel).namelist()

    assert "zmart_analysis/engine.py" in names
    assert "zmart_analysis/environments.py" in names
    assert not [name for name in names if "workflows/" in name]
