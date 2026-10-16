"""The rules the workflows are built by, checked on their files.

- A workflow is self-contained: its steps import only standard Python,
  installed packages and the other steps of the same workflow.
- ``read_images`` is the only step that opens an image file; every other
  step takes its pixels from it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"

STEP_FILES = sorted(WORKFLOWS.glob("*/steps/*.py"))

#: The calls that open an image file.
READS_AN_IMAGE = {"imread", "TiffFile", "open_ome_zarr_container", "open_group", "load_plane"}


def _imported_modules(path: Path) -> set[str]:
    """The top-level names of everything a step file imports."""

    tree = ast.parse(path.read_text(encoding="utf-8"))

    names = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}

        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])

    return names


def _called_names(path: Path) -> set[str]:
    """The names of every function a step file calls: ``f()`` and ``x.f()``."""

    tree = ast.parse(path.read_text(encoding="utf-8"))

    calls = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                calls.add(node.func.id)

            elif isinstance(node.func, ast.Attribute):
                calls.add(node.func.attr)

    return calls


def test_there_are_two_workflows_and_nothing_shared():
    assert sorted(
        path.name for path in WORKFLOWS.iterdir() if path.is_dir() and path.name != "__pycache__"
    ) == [
        "alignment",
        "object_analysis",
    ]


@pytest.mark.parametrize("step", STEP_FILES, ids=lambda path: f"{path.parents[1].name}/{path.stem}")
def test_a_step_imports_only_from_its_own_workflow(step):
    """A name that is a step of another workflow, or a workflow, is not imported."""

    own = {path.stem for path in step.parent.glob("*.py")}

    elsewhere = {path.stem for path in STEP_FILES if path.parent != step.parent} - own

    workflows = {path.name for path in WORKFLOWS.iterdir() if path.is_dir()}

    assert not _imported_modules(step) & (elsewhere | workflows | {"shared", "parts"})


@pytest.mark.parametrize("step", STEP_FILES, ids=lambda path: f"{path.parents[1].name}/{path.stem}")
def test_only_read_images_opens_an_image(step):

    if step.stem == "read_images":
        assert _called_names(step) & READS_AN_IMAGE
    else:
        assert not _called_names(step) & READS_AN_IMAGE


@pytest.mark.parametrize("step", STEP_FILES, ids=lambda path: f"{path.parents[1].name}/{path.stem}")
def test_every_step_names_its_environment(step):
    """The environment is the workflow's own: ZMART--<workflow>--<name>."""

    tree = ast.parse(step.read_text(encoding="utf-8"))

    metadata = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", None) == "METADATA" for t in node.targets)
    )

    assert metadata["environment"].startswith(f"ZMART--{step.parents[1].name}--")


def test_a_step_with_a_dataclass_loads_through_the_test_helper():
    """A dataclass looks its module up in ``sys.modules`` while it is built.

    ``measure_objects`` defines two; loading it by path must therefore put
    the module there under its own name, as the engine's worker does.
    """

    import sys

    from helpers import load_step

    module = load_step("object_analysis", "measure_objects")

    assert sys.modules[module.__name__] is module
    assert module.__name__ == "object_analysis_measure_objects"
