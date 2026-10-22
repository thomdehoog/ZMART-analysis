"""The workflows travel with the package, so an install from GitHub can run them.

A pipeline is found by its name, and a workflow's step environment is made by
naming the workflow, wherever the package is installed.
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from zmart_analysis import environments, pipeline_file

REPO = Path(__file__).resolve().parents[1]


def test_a_shipped_pipeline_is_found_by_its_name():
    path = pipeline_file("object_analysis_watershed")

    assert path.is_file()
    assert path.name == "object_analysis_watershed.yaml"
    assert path.parent.parent.name == "object_analysis"


def test_an_unknown_pipeline_names_the_ones_there():
    with pytest.raises(ValueError, match="object_analysis_watershed"):
        pipeline_file("no_such_pipeline")


def test_a_workflow_environment_is_made_by_naming_the_workflow():
    """``python -m zmart_analysis.environments object_analysis --step classical`` runs its setup_env.py."""
    with mock.patch("runpy.run_path") as run:
        environments.main(["object_analysis", "--step", "classical"])

    script = Path(run.call_args.args[0])
    assert script.name == "setup_env.py"
    assert script.parent.parent.name == "object_analysis"
    assert run.call_args.kwargs["run_name"] == "__main__"


@pytest.mark.slow
def test_the_wheel_carries_the_workflows(tmp_path):
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", str(REPO), "--no-deps", "--no-build-isolation",
         "-w", str(tmp_path), "-q"],
        check=True,
    )
    wheel = next(tmp_path.glob("zmart_analysis-*.whl"))
    names = zipfile.ZipFile(wheel).namelist()

    for shipped in (
        "zmart_analysis/workflows/object_analysis/pipelines/object_analysis_watershed.yaml",
        "zmart_analysis/workflows/object_analysis/steps/build_object_table.py",
        "zmart_analysis/workflows/object_analysis/environments/setup_env.py",
    ):
        assert shipped in names, shipped
