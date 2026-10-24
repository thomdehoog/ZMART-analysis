"""Every example step runs in an environment its workflow describes.

A step names its environment in its ``METADATA``; the workflow's
``environments/`` folder holds a conda environment file of that name, and a
locked one points at a lock file that holds every package at one version.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"
STEPS = sorted(WORKFLOWS.glob("*/steps/*.py"))


def _environment_of(step: Path) -> str:
    """The ``environment`` in a step file's ``METADATA``, read without importing the step."""
    for node in ast.parse(step.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "METADATA" for t in node.targets
        ):
            return ast.literal_eval(node.value)["environment"]
    raise AssertionError(f"{step} has no METADATA")


def _environment_files(workflow: Path) -> dict[str, Path]:
    files = sorted((workflow / "environments").glob("*.yml"))
    return {yaml.safe_load(path.read_text(encoding="utf-8"))["name"]: path for path in files}


@pytest.mark.parametrize("step", STEPS, ids=lambda step: f"{step.parents[1].name}/{step.stem}")
def test_a_steps_environment_has_its_file(step):
    workflow = step.parents[1]
    assert _environment_of(step) in _environment_files(workflow)


@pytest.mark.parametrize(
    "environment_file", sorted(WORKFLOWS.glob("*/environments/*.yml")), ids=lambda path: path.stem
)
def test_a_locked_environment_pins_every_package(environment_file):
    described = yaml.safe_load(environment_file.read_text(encoding="utf-8"))
    pip = next((item["pip"] for item in described["dependencies"] if isinstance(item, dict)), [])
    locks = [line.split(maxsplit=1)[1] for line in pip if line.startswith("-r ")]
    if not locks:
        pytest.skip(f"{environment_file.name} is not locked")
    lock = environment_file.parent / locks[0]
    lines = [
        line.strip()
        for line in lock.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines
    for line in lines:
        assert re.fullmatch(r"[A-Za-z0-9_.\-]+==[^=\s]+", line), line
