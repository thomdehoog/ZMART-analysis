# 2. Implement an analysis step

This page shows how to write a step for the ZMART Analysis engine. For a
guided example, open the [tutorial notebook](tutorial.ipynb). How to run a
pipeline is in [part 1](../1_use_the_engine/README.md).

## The smallest step

A step is one Python file with one function, `run`. This is
`steps/double_it.py`:

```python
"""double_it -- twice the number it is given."""

import math

METADATA = {
    "description": "Double a number",
    "version": "1.0",
    "environment": "ZMART--hello--main",
}


def run(pipeline_data, state, **params):
    n = pipeline_data["input"]["n"]
    pipeline_data["double_it"] = {"value": _twice(n)}
    return pipeline_data


def _twice(n):
    return math.fsum([n, n])
```

Every step file has the same order: a docstring that says what the step
does, what it takes and what it hands on; the imports; `METADATA`; `run`;
and then the step's own helpers. `run` only combines the helpers, one call
to a line with a blank line between, so it reads as the recipe of the step.
A helper's name starts with `_`: only this step uses it. Each helper opens
with a short docstring, and a comment sits on the line above the code it
explains. Where a package already does the work, call the package.

The imports can stay at the top. The engine reads `METADATA` as text and never
imports the file; only the worker in the step's environment does.

A pipeline that uses it, `pipelines/hello.yaml`:

```yaml
metadata:
  functions_dir: "../steps"

hello:
  - double_it:
```

The step's name is its file name without `.py`. The pipeline looks for the
file in `functions_dir`.

## What `run` receives and returns

```python
def run(pipeline_data: dict, state: dict, **params) -> dict:
```

| Argument | What it is |
|---|---|
| `pipeline_data` | The job. `["input"]` is what was passed to `engine.submit`. `["metadata"]` is what the engine knows about the job. Every earlier step has added its output under its own name. |
| `state` | A dictionary that this step keeps from one job to the next. It is empty on the first job. See [Keep a model loaded](#keep-a-model-loaded). |
| `**params` | The parameters listed under this step in the pipeline. |

**Add your output to `pipeline_data` under the step's name, and return
`pipeline_data`.** The next step receives it. A step that returns anything
other than a dictionary fails its job.

**Return plain Python and NumPy arrays.** Numbers, strings, lists,
dictionaries and arrays reach the next step in any environment, also one
with another NumPy version. Other objects, such as a pandas table, still
work, but only between environments with the same version of that library.

The last step of a workflow may use the workflow's name as its key instead,
when that is the result everyone will ask for. `build_object_table` puts
the object table under `object_analysis`.

If your step raises an error, only its own job fails. On the job board,
`engine.status(name)`, the job is `failed`, with the step and the error.

A few names are the engine's own, in what it hands a step: `input`,
`metadata`, `provenance`, `lineage`, `parts`, `failures` and `previous`. A
step cannot be called that; register refuses it.

Steps that follow each other in one environment, with the same
`max_workers`, run together in one worker, and the output of one reaches
the next in memory, as it is. Only the last output of such a run is
written to a file, so every output must still be one that can be written:
plain Python and NumPy arrays, as above.

`pipeline_data["metadata"]` holds:

| Key | What it is |
|---|---|
| `workflow_name` | The name of the pipeline's step list, for example `focus`. |
| `yaml_filename` | The pipeline's file name. |
| `steps` | The names of the steps that run with this one, in order. |
| `scope` | The `scope` given at submit, for example `{"carrier": 1, "compartment": 3}`. |
| `submission_idx` | The job's id on the board, counted from 1. |
| `output` | The pipeline's folder, `<output>/analysis/<name>/`. A step that keeps a file writes it here. |
| `datetime` | When the job started, as `YYYYMMDD-HHMMSS`. |
| `verbose` | The pipeline's `verbose` setting. Print progress when it is above `0`, and nothing when it is `0`. |

## METADATA

Every step file has a `METADATA` dictionary. It tells the engine how to
run the step:

```python
METADATA = {
    "description": "Score plane sharpness in a z-stack and find the peak",
    "version": "1.0",
    "environment": "ZMART--focus--main",
    "max_workers": 1,
}
```

| Key | What it does | Default |
|---|---|---|
| `environment` | The conda environment the step runs in. Required: the engine refuses a step without it. | |
| `max_workers` | How many copies of the step may run at once, whatever the steps around it. | `1` |
| `description`, `version` | Notes for the reader. Every shipped step has them. | |

The engine reads `METADATA` as text, without running the file. So write it
as plain values at the top level of the file, not as something that is
computed.

A pipeline can override `max_workers`, but not `environment`. The engine
refuses a pipeline that tries. If a step has to run in two environments, make
two step files.

## Give a step its own environment

Steps often need packages that do not install together. Each step can
therefore run in its own conda environment. The step sees the packages of
that environment and no others.

### Name it

Environments are named `ZMART--<workflow>--<name>`. A workflow with one
environment calls it `main`. `object_analysis` has three: `cellpose`,
`classical` and `umap`. A run that only uses the classical steps then never
has to load PyTorch.

### Describe it

Each workflow has a short script, `environments/setup_env.py`, that lists
what the environment needs:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from zmart_analysis.environments import setup_workflow_env

if __name__ == "__main__":
    setup_workflow_env(
        workflow="intensity",              # the workflow folder's name
        pip_packages=["numpy"],            # as pip spells them, e.g. "cellpose>=4,<5"
        diagnostics=[("numpy imports", "import numpy; print(numpy.__version__)")],
        install_torch=False,               # True installs PyTorch for the detected GPU
    )
```

`diagnostics` are small checks that run inside the new environment. Each is
a label and one line of Python that prints a short result.

Three more parameters are optional: `python_version` (default `"3.12"`),
`default_step` (the environment made when `--step` is not given, default
`"main"`) and `steps_dir`. With `steps_dir` set to the workflow's `steps/`
folder, `__STEPS__` in a check is replaced by that folder, so the check can
import a step.

A workflow with several environments keeps one package list per
environment and picks one from `--step`.
`workflows/object_analysis/environments/setup_env.py` is the example.

### Make it

```bash
python workflows/focus/environments/setup_env.py                   # makes ZMART--focus--main
python workflows/object_analysis/environments/setup_env.py --step classical
```

| Option | What it does |
|---|---|
| `--step <name>` | Which of the workflow's environments to make. |
| `--python <version>` | The Python version. Default 3.12. |
| `--gpu cu128\|cu124\|cu121\|mps\|cpu` | Which PyTorch build to install, for workflows that use it. By default the script detects your GPU. |
| `--check` | Run the checks on an existing environment. Nothing is installed. |
| `--dry-run` | Print the commands without running them. |

The environment is built from conda-forge, and the packages are installed
with pip inside it. An environment that already exists is left alone. To
rebuild one, remove it first with `clean_env.py --step <name>`.

## Keep a model loaded

Loading a model takes seconds. Using it takes milliseconds. A worker stays
running between jobs, and `state` is where your step keeps things for the
next job:

```python
def run(pipeline_data, state, **params):
    if "model" not in state:
        from cellpose import models
        state["model"] = models.CellposeModel(gpu=params.get("gpu", True))
    masks, *_ = state["model"].eval(image)
    ...
```

Two things to know:

- Each worker has its own `state`. With `max_workers: 2`, each of the two
  copies loads its own model.
- A worker keeps its `state` until its pipeline is deregistered (and no
  other pipeline needs the worker) or the engine shuts down. The next job
  after that starts with an empty `state`.

## A step that waits for a scope

A step with a `scope` in the pipeline does not run per image. It runs once
per unit, for example once per compartment, when that unit is complete.
[Part 1](../1_use_the_engine/README.md#steps-that-wait-for-a-group-of-images)
explains scopes and shows such a pipeline.

A scoped step receives a different `pipeline_data`. There is no `input`;
there are the **parts** of its unit:

```text
parts ──► your step ──► a new dictionary ──► a part one level up
```

Every part has the same outside, whether it comes from one image or from a
whole well:

```python
part = {
    "id": 12,                                          # the job, as on the board
    "scope": {"compartment": 3, "position": 7},        # where it is from
    "output": {"segment": {...}, "measure": {...}},    # what the steps made
}
```

The engine fixes this outside; what is inside `output` is yours. An image's
part also carries the `input` it was submitted with.

| Key | What it is |
|---|---|
| `parts` | One part per image of the unit, in submission order. For a wider scope, one part per narrower unit instead: a carrier step receives one part per compartment, its `scope` the compartment and its `id` the compartment's scoped job. Each part is read from disk when the step reaches it, so a large unit never sits in memory at once; loop over it, or make a `list(...)` of it to use it twice. |
| `failures` | What failed in this unit, each as `{"id", "scope", "step", "error"}`: the job that failed, where, in which step, and why. |
| `metadata["unit"]` | Which unit this is, widest level first, for example `{"carrier": 1, "compartment": 3}`. It is `{}` for a step over everything. |
| `metadata["scope_level"]` | The level, for example `"compartment"`. |
| `metadata["scope"]` | The `scope` of the image that completed the unit. |
| `metadata["n_accumulated"]`, `metadata["n_failures"]` | How many images and how many failures. |

A failure with `"step": "engine"` did not come from a step. It is the
engine's own note: an image of a narrower unit that was never completed.
Completing a level completes the narrower units inside it, so this happens
only when the pipeline cannot tell that a level is narrower: list your levels
under `levels`.

**Return a new dictionary with your output.** Do not copy the parts into
it; that only uses memory. The engine adds `metadata` and `lineage` to your
result afterwards. A later step with the same scope receives what you
returned.

```python
def run(pipeline_data, state, **params):
    counts = [part["output"]["segment"]["n_cells"] for part in pipeline_data["parts"]]
    return {"total": sum(counts), "images": len(counts)}
```

The scoped steps in `tests/test_parts.py` and `tests/test_scopes.py`, and
`summarise_group` in this part's tutorial, are worked examples.

## A step over time

A step with `timepoints: 2` or more
([part 1](../1_use_the_engine/README.md#steps-that-look-over-time)) runs once
per timepoint of its place, in timepoint order. It receives its parts
grouped by timepoint, oldest first, and its own output at the timepoint
before:

| Key | What it is |
|---|---|
| `parts` | `[{"timepoint": 3, "parts": [...]}, {"timepoint": 4, "parts": [...]}]`: the timepoints of its place within the window. A round the place never had is missing, not empty. |
| `previous` | What this step returned for the same place at the timepoint before, or `None`: at the first timepoint, or when that run failed. |
| `metadata["unit"]` | Its place and this timepoint, for example `{"compartment": 3, "position": 7, "timepoint": 4}`. |

`previous` is what a tracker needs to keep each cell's id from one
timepoint to the next:

```python
def run(pipeline_data, state, **params):
    *before, now = pipeline_data["parts"]
    cells = now["parts"][0]["output"]["segment"]["cells"]
    previous = pipeline_data["previous"]
    ids = match(cells, previous["cells"]) if previous else new_ids(cells)
    return {"cells": cells, "ids": ids}
```

The time steps in `tests/test_time.py` are worked examples.

## Parameters

The parameters under a step in the pipeline arrive in `**params`.

Give every parameter a value in the pipeline, with a comment. The pipeline is
the record of what was done, and a reader should not need to open the step
file to understand it.

A step may let a single job override a parameter. It then looks in the
job's input first:

```python
def _setting(inp, params, key):
    return inp.get(key, params.get(key, None))
```

`segment_cellpose` does this, so an operator can tune detection for one
position. The pipeline holds the default, and a job that carries `diameter`
uses its own value for that image only. Say in the docstring which
parameters can be overridden this way.

## The folder of a workflow

```text
workflows/object_analysis/
├── README.md              what the workflow does and how to choose its settings
├── pipelines/             the pipelines (YAML)
├── steps/                 one Python file per step
├── environments/
│   ├── setup_env.py       makes the environments
│   └── clean_env.py       removes them again
└── tests/
```

A workflow needs nothing outside its folder: two workflows share no code.
A step may use another step of its own workflow. The engine loads a step
file on its own, not as part of a package, so the step first adds its own
folder to Python's search path:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from segment_cellpose import segment
```

The first step of every pipeline that works on images is `read_images`. It
opens the files and hands on the pixels, so no step after it opens a file.

To add a workflow, copy `focus`. It is the smaller one. Then:

1. Write the steps.
2. Name each environment and list its packages in `setup_env.py`.
3. Write a pipeline that holds every parameter, each with a comment.
4. Add tests and a short `README.md`.

## Test a step

A step is an ordinary function, so a test can call it without the engine:

```python
from double_it import run


def test_double_it():
    out = run({"input": {"n": 21}}, {})
    assert out["double_it"]["value"] == 42
```

Tests live in `workflows/<name>/tests/`. They can import a step by its
name, because `workflows/conftest.py` puts every `steps/` folder on the
search path.

To test a scoped step, pass it its parts by hand:

```python
def test_summarise():
    parts = [{"id": 1, "scope": {"compartment": 3}, "output": {"segment": {"n_cells": 5}}}]
    out = run({"parts": parts, "failures": [], "metadata": {"unit": {"compartment": 3}}}, {})
    assert out["total"] == 5
```

Some tests need something your computer may not have. They carry a marker,
so you can leave them out:

| Marker | Needs |
|---|---|
| `cellpose` | The `ZMART--object_analysis--cellpose` environment, with Cellpose and its model. |
| `conda_env` | The conda environments that a workflow's `setup_env.py` makes. |
| `pooch` | Public sample images, downloaded on first use. |
| `slow` | More than about five seconds. |

```bash
pytest -m "not cellpose and not conda_env and not pooch"   # what CI runs
pytest workflows/focus                                      # one workflow
```

`workflows/focus/tests/test_score_focus.py` shows the pattern. It scores
a made-up z-stack directly and then, if `ZMART--focus--main` exists, once
more through the engine. The `*real_samples.py` tests in each workflow run the
pipelines on real microscopy from scikit-image's sample data.

When you try an edited step through the engine, make a new `Engine` first.
A running worker keeps the code it loaded.
