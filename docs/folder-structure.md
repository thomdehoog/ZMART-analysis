# Folder structure

```text
ZMART-analysis/
├── engine/                     the engine itself; you rarely need to open it
├── workflows/
│   ├── _image_io.py            reads a plane from OME-Zarr or OME-TIFF, for every step
│   ├── _focus_metrics.py       the sharpness measures
│   ├── _population.py          how a population of objects is summarised
│   ├── focus/
│   ├── object_analysis/
│   └── driver_configuration/
├── docs/                       these pages
├── conftest.py                 shared test settings
└── pyproject.toml              package and test settings
```

## A workflow folder

Every workflow has the same four parts:

```text
workflows/object_analysis/
├── pipelines/                  the recipes (YAML)
│   ├── object_analysis.yaml
│   └── object_analysis_scoped.yaml
├── steps/                      one Python file per step
│   ├── detect_objects.py
│   └── ...
├── environments/
│   ├── setup_env.py            creates the conda environments the steps name
│   └── clean_env.py            removes them again
└── tests/                      tests for the steps
```

A recipe finds its steps through `functions_dir: "../steps"`, so a recipe
and the steps it uses always sit in the same workflow.

## The shared modules

Files in `workflows/` whose names start with an underscore are shared by
more than one workflow. A step imports them after adding `workflows/` to
Python's search path:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _image_io import load_plane
```

Keeping one copy means a fix reaches every step that uses it.

## Adding a workflow

1. Copy the four folders of an existing workflow and rename it.
2. Write the steps; see [Writing a step](writing-a-step.md).
3. Name each step's environment `ZMART--<workflow>--<purpose>` and list its
   packages in `environments/setup_env.py`.
4. Write a recipe in `pipelines/`, and tests in `tests/`.

## Running the tests

```bash
pytest -m "not cellpose and not conda_env and not pooch"   # what CI runs
pytest workflows/focus                                      # one workflow
```

Tests marked `cellpose` need Cellpose installed, `conda_env` need the
workflow environments, and `pooch` download public sample images. Each
skips cleanly when what it needs is missing.
