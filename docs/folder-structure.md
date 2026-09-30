# Folder structure

```text
ZMART-analysis/
├── engine/                     the engine itself; you rarely need to open it
│   ├── engine.py               the Engine you call: register, submit, status, results
│   ├── pipeline.py             reads a recipe, splits it into phases, tracks scopes
│   ├── workers.py              the processes that run the steps, and their errors
│   ├── worker_script.py        runs inside each step's conda environment
│   └── conda_utils.py          finds conda; creates and removes environments
├── workflows/
│   ├── shared/                 helpers more than one workflow uses
│   │   ├── image_io.py         reads a plane from OME-Zarr or OME-TIFF
│   │   ├── focus_metrics.py    the sharpness measures
│   │   └── population.py       how a population of objects is summarised
│   ├── focus/
│   ├── object_analysis/
│   └── driver_configuration/
├── tests/                      tests for the engine and the shared helpers
├── docs/                       these pages
├── conftest.py                 shared test settings
└── pyproject.toml              package and test settings
```

A few files are called `__init__.py`. Python needs that exact name, two
underscores on each side, to treat a folder as a package it can import
from. They hold only a short description of their folder.

## A workflow folder

Every workflow has the same four folders and a README:

```text
workflows/object_analysis/
├── README.md                   what the workflow does and how to choose its settings
├── pipelines/                  the recipes (YAML)
│   ├── object_analysis.yaml
│   └── object_analysis_scoped.yaml
├── steps/                      one Python file per step
│   ├── detect_objects.py
│   └── ...
├── environments/
│   ├── setup_env.py            lists the packages each environment needs
│   └── clean_env.py            removes the environments again
└── tests/                      tests for the steps
```

A recipe finds its steps through `functions_dir: "../steps"`, so a recipe
and the steps it uses always sit in the same workflow.

## The shared helpers

The files in `workflows/shared/` are used by more than one workflow. A step
imports them after adding `workflows/` to Python's search path:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from shared.image_io import load_plane
```

Keeping one copy means a fix reaches every step that uses it.

## Adding a workflow

1. Copy an existing workflow folder and rename it.
2. Write the steps; see [Writing a step](writing-a-step.md).
3. Name each step's environment `ZMART--<workflow>--<purpose>` and list its
   packages in `environments/setup_env.py`. The script only lists packages
   and a few checks; the work of creating the environment is shared, in
   `engine/conda_utils.py`.
4. Write a recipe in `pipelines/`, tests in `tests/`, and a short
   `README.md` that says what the workflow is for.

## Running the tests

```bash
pytest -m "not cellpose and not conda_env and not pooch"   # what CI runs
pytest tests                                                # the engine and shared helpers
pytest workflows/focus                                      # one workflow
```

Tests marked `cellpose` need Cellpose installed, `conda_env` need the
workflow environments, and `pooch` download public sample images. Each
skips cleanly when what it needs is missing.
