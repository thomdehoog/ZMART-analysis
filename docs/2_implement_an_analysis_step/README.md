# 2. Implement an analysis step

How to write a step for the ZMART Analysis engine. This page is the
reference. The [tutorial](tutorial.ipynb), a notebook, walks you through writing one.

## Contents

1. [The idea](#the-idea)
2. [The smallest step](#the-smallest-step)
3. [What `run` receives and returns](#what-run-receives-and-returns)
4. [METADATA](#metadata)
5. [Give a step its own environment](#give-a-step-its-own-environment)
6. [Keep a model loaded](#keep-a-model-loaded)
7. [A step over a scope](#a-step-over-a-scope)
8. [Parameters](#parameters)
9. [Provenance](#provenance)
10. [The folder of a workflow](#the-folder-of-a-workflow)
11. [Test a step](#test-a-step)

## The idea

A step is one Python file with one function, `run`. A recipe (YAML) says
which steps run, in which order, with which parameters. The engine starts a
worker in the conda environment the step asks for, hands it the image, and
passes what the step returns to the next step.

```
  recipe (YAML) ──► engine ──► worker in the step's environment ──► run(...)
```

A step is a plain function. A test or a notebook can call it without the
engine. The engine never imports the step; the heavy imports happen in the
worker.

How recipes are registered and images submitted is in
[part 1](../1_use_the_engine/README.md).

## The smallest step

`steps/double_it.py`:

```python
def run(pipeline_data, state, **params):
    n = pipeline_data["input"]["n"]
    pipeline_data["double_it"] = {"value": n * 2}
    return pipeline_data
```

A recipe that uses it, `pipelines/hello.yaml`:

```yaml
metadata:
  functions_dir: "../steps"

hello:
  - double_it:
```

The step's name is its file name without `.py`. The recipe finds it in
`functions_dir`, relative to the recipe. This step has no `METADATA`, so it
runs in the engine's own environment, one copy at a time.

## What `run` receives and returns

```python
def run(pipeline_data: dict, state: dict, **params) -> dict:
```

| Argument | What it is |
|---|---|
| `pipeline_data` | `["input"]` is what was passed to `engine.submit`. `["metadata"]` is what the engine knows about the job. Each earlier step has added its output under its own name. |
| `state` | A dictionary for this step in this worker. It survives from one job to the next. Empty on the first job. Keep slow things here: a loaded model, an open file. |
| `**params` | The parameters under this step in the recipe. |

**Return `pipeline_data`** with your output added under your step's name.

A step that returns anything but a dictionary fails its job. A step that
raises fails only its own job. The message and traceback appear in
`engine.status()`.

`pipeline_data["metadata"]` on a per-image job:

| Key | What it is |
|---|---|
| `workflow_name` | The top key of the recipe, e.g. `focus`. |
| `yaml_filename` | The recipe's file name. |
| `steps` | The step names in this phase, in order. |
| `scope` | The `scope` given at submit, e.g. `{"carrier": 1, "compartment": 3}`. |
| `submission_idx` | The job's number, from zero, in submission order. |
| `datetime` | When the job started, `YYYYMMDD-HHMMSS`. |
| `verbose` | The recipe's `metadata.verbose`. Default 2. |

## METADATA

A step may carry a `METADATA` dictionary at the top of the file:

```python
METADATA = {
    "description": "Score plane sharpness in a z-stack and find the peak",
    "version": "1.0",
    "environment": "ZMART--focus--main",
    "max_workers": 1,
}
```

The engine reads it **without running the file**. It parses the text and
takes the literal dictionary assigned to `METADATA`. So it must be a plain
literal, at the top level of the file.

| Key | What it does | Default |
|---|---|---|
| `environment` | The conda environment the step runs in. | The engine's own. |
| `max_workers` | How many copies may run at once. | 1 |

`description` and `version` are for the reader. Every shipped step has
them.

A recipe may override `max_workers`. A recipe may **not** set
`environment`; the engine refuses it. A step that must run in two
environments is two step files.

## Give a step its own environment

Name it in `METADATA`:

```python
METADATA = {"environment": "ZMART--object_analysis--cellpose"}
```

Names follow `ZMART--<workflow>--<step>`: the workflow folder, then a short
word for the environment. A workflow with one environment calls it `main`.
`object_analysis` has `cellpose` and `classical`, so a run that only needs
the second does not pay for the first.

The workflow's `environments/setup_env.py` lists the packages and a few
checks. It hands the work to `engine/conda_utils.py`:

```bash
python workflows/focus/environments/setup_env.py                   # makes ZMART--focus--main
python workflows/object_analysis/environments/setup_env.py --step cellpose
```

| Option | What it does |
|---|---|
| `--step <name>` | Which of the workflow's environments to make. |
| `--python <version>` | Python version. Default 3.12. |
| `--gpu cu128\|cu124\|cu121\|mps\|cpu` | Which PyTorch build, for workflows that use torch. Default: detect. |
| `--check` | Run the checks on an existing environment. Installs nothing. |
| `--dry-run` | Print the commands without running them. |

Every environment is built from conda-forge; packages are installed with
pip inside it. `clean_env.py` beside it removes the environments again.

The engine starts a worker with `conda run -n <environment>`. The step sees
that environment's packages and no other. A step that names the engine's
own environment runs in the engine's interpreter.

## Keep a model loaded

Loading a model takes seconds. Using it takes milliseconds. A worker stays
running, and `state` is what it keeps for your step between jobs:

```python
def run(pipeline_data, state, **params):
    if "model" not in state:
        from cellpose import models
        state["model"] = models.CellposeModel(gpu=params.get("gpu", True))
    masks, *_ = state["model"].eval(image)
    ...
```

`state` is per step and per worker. Two copies of a step (`max_workers: 2`)
each load their own model. A worker idle longer than the engine's
`idle_timeout` is stopped, and the next job starts with an empty `state`.

## A step over a scope

A step with a `scope` in the recipe does not run per image. It waits until
the acquisition says the unit is complete, then runs once over everything
collected for it:

```yaml
object_analysis:
  - detect_objects:            # every image, as soon as it lands
  - extract_classical_features:
  - build_object_table:
  - summarise_population:      # each compartment, once it is complete
      scope: compartment
  - compare_populations:       # each carrier, once it is complete
      scope: carrier
```

A step over a wider scope collects the results of the step over the
narrower one. `compare_populations` receives one result per compartment,
not per image.

Such a step receives a different `pipeline_data`:

| Key | What it is |
|---|---|
| `results` | One entry per image, or per narrower unit, in submission order: what the previous phase returned. |
| `failures` | What failed under this unit. Each has `scope`, `step`, `error`, `phase` and `submission_idx`. A `step` of `"engine"` means an image of a narrower unit that was not closed when this one was. |
| `metadata["unit"]` | Which unit, widest level first, e.g. `{"carrier": 1, "compartment": 3}`. `{}` for a step over everything. |
| `metadata["scope_level"]` | The level, e.g. `"compartment"`. |
| `metadata["scope"]` | The scope of the image that closed the unit. It may name narrower levels too. |
| `metadata["n_accumulated"]`, `metadata["n_failures"]` | How many of each. |

There is no `input`.

**Return a new dictionary** with your output. Copying the per-image results
forward only uses memory. The engine adds `lineage` to your result after it
returns: the images under this unit, the failures under it, and where every
step below ran. You carry nothing over.

`workflows/object_analysis/steps/summarise_population.py` is the worked
example.

## Parameters

Recipe parameters arrive as `**params`. Give every parameter a default in
the recipe, with a comment. The recipe is the record; a reader should not
need the step file.

A step may let one submission override a recipe parameter by looking in
`pipeline_data["input"]` first:

```python
def _setting(inp, params, key):
    return inp.get(key, params.get(key, None))
```

This is how `detect_objects` lets the operator tune detection on one
position. The recipe holds the default; a submission carrying `diameter`
wins for that image only. Say in the docstring which parameters allow this.

## Provenance

Before a result leaves the worker, the engine adds where it ran, under
`pipeline_data["provenance"][<step name>]`:

| Key | What it is |
|---|---|
| `environment` | The conda environment's name. |
| `python` | The interpreter version. |
| `fingerprint` | A hash of every installed package and version. Same fingerprint, identical environment. |
| `packages` | The version of every package this worker imported. |

The recipe says what was asked for. `provenance` says what ran. The step
does nothing for this.

## The folder of a workflow

```text
workflows/object_analysis/
├── README.md                   what the workflow does and how to choose its settings
├── pipelines/                  the recipes (YAML)
├── steps/                      one Python file per step
├── environments/
│   ├── setup_env.py            the packages each environment needs
│   └── clean_env.py            removes the environments again
└── tests/
```

Helpers more than one workflow uses live in `workflows/shared/`. A step
file is loaded on its own, not as part of a package, so it adds
`workflows/` to the search path first:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from shared.image_io import load_plane
```

To add a workflow: copy `focus`, the smallest. Write the steps. Name each
environment `ZMART--<workflow>--<step>` and list its packages in
`setup_env.py`. Write a recipe with every parameter and a comment. Add
tests and a short `README.md`.

## Test a step

A step is an ordinary function. A test calls it directly:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "steps"))
from double_it import run


def test_double_it():
    out = run({"input": {"n": 21}}, {})
    assert out["double_it"]["value"] == 42
```

For a step over a scope, pass `{"results": [...], "failures": [],
"metadata": {"unit": {...}, "scope_level": "compartment"}}`.

Tests that need something the machine may not have mark themselves, so
they skip rather than fail:

| Marker | Needs |
|---|---|
| `cellpose` | Cellpose and scikit-image in the active environment. |
| `conda_env` | The conda environments the workflow's `setup_env.py` makes. |
| `pooch` | Public sample images, downloaded on first use. |
| `slow` | More than about five seconds. |

```bash
pytest -m "not cellpose and not conda_env and not pooch"   # what CI runs
pytest workflows/focus                                      # one workflow
```

`workflows/focus/tests/test_focus.py` shows the pattern. It scores a
synthetic z-stack directly, then, when `ZMART--focus--main` exists, once
more through the engine.
