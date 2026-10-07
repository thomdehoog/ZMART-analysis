# 2. Implement an analysis step

Write one Python function, and the engine runs it in the right environment,
on every image or on every completed unit of the sample.

This page is the documentation of what a step is and how it is written. For a
step-by-step walk-through, see the [tutorial](tutorial.md).

## Contents

1. [The idea](#the-idea)
2. [The smallest step](#the-smallest-step)
3. [What `run` receives and returns](#what-run-receives-and-returns)
4. [METADATA: what the engine reads from the file](#metadata-what-the-engine-reads-from-the-file)
5. [Give a step its own environment](#give-a-step-its-own-environment)
6. [Keep a model loaded](#keep-a-model-loaded)
7. [A step over a scope](#a-step-over-a-scope)
8. [Parameters: the recipe, and overrides per submission](#parameters-the-recipe-and-overrides-per-submission)
9. [What a result carries back](#what-a-result-carries-back)
10. [The folder structure of a workflow](#the-folder-structure-of-a-workflow)
11. [Add a workflow](#add-a-workflow)
12. [Test a step](#test-a-step)
13. [Rules every step follows](#rules-every-step-follows)

## The idea

An analysis pipeline is a chain of steps: detect the objects, measure them,
summarise the population. In ZMART Analysis every step is **one Python file
with one function, `run`**. A *recipe*, a YAML file, says which steps run in
which order and with which parameters, and the engine does the rest: it starts
a worker in the conda environment the step asks for, hands it the image, and
passes what the step returns on to the next step.

```
  recipe (YAML) ──► engine ──► worker in the step's environment ──► run(...)
```

Because a step is a plain function, you can call it directly from a test or a
notebook, without the engine at all. Because the environment is named in the
file, the engine never imports the step itself; it only reads the name. The
heavy imports happen inside the worker, where they belong.

How a recipe is registered, how images are submitted and how results are read
back is in [Use the engine](../1_use_the_engine/README.md).

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

The step's name is its file name without `.py`. The recipe finds the file in
`functions_dir`, which is relative to the recipe itself; `"../steps"` is also
the default, so a recipe and its steps always sit in the same workflow folder.

This step has no `METADATA`, so it runs in the environment you started the
engine from, one copy at a time.

## What `run` receives and returns

```python
def run(pipeline_data: dict, state: dict, **params) -> dict:
```

| Argument | What it is |
|---|---|
| `pipeline_data` | A dictionary. `pipeline_data["input"]` is exactly what was passed to `engine.submit`. `pipeline_data["metadata"]` is what the engine knows about this job (below). Each earlier step has added its output under its own name. |
| `state` | A dictionary that belongs to this step, in this worker, and survives from one job to the next. Empty on the worker's first job. Use it for anything slow to make: a loaded model, an opened file. |
| `**params` | The parameters written under this step in the recipe, as keyword arguments. |

**Return `pipeline_data`**, with your output added under your step's name.
That is where the next step and, in the end, the person reading the results
will look for it. A step that returns anything other than a dictionary fails
its job.

`pipeline_data["metadata"]` on a per-image job holds:

| Key | What it is |
|---|---|
| `workflow_name` | The top-level key of the recipe, for example `focus`. |
| `yaml_filename` | The recipe's file name. |
| `steps` | The names of the steps in this phase, in order. |
| `scope` | The `scope` dictionary given at submission, for example `{"carrier": 1, "compartment": 3}`. |
| `submission_idx` | The job's number, counting from zero in submission order. |
| `datetime` | When the job started, as `YYYYMMDD-HHMMSS`. |
| `verbose` | The recipe's `metadata.verbose`, default 2. Steps use it to decide how much to print. |

A step that raises an error fails only its own job. The error message and
traceback appear in `engine.status()`, and the other jobs carry on.

## METADATA: what the engine reads from the file

A step can carry a `METADATA` dictionary at the top of the file:

```python
METADATA = {
    "description": "Score plane sharpness in a z-stack and find the peak",
    "version": "1.0",
    "environment": "ZMART--focus--main",
    "max_workers": 1,
}
```

The engine reads this **without running the file**: it parses the file as
text and reads the literal dictionary assigned to the name `METADATA`. So
`METADATA` must be a plain literal (strings, numbers, `True`, `None`), not an
expression, and must be assigned at the top level of the file. Everything
else in the file, including the imports, is only ever executed inside the
worker.

The engine uses two keys:

| Key | What it does | Default |
|---|---|---|
| `environment` | The conda environment the step runs in. | None: the environment the engine was started from. |
| `max_workers` | How many copies of this step may run at the same time. A whole number of 1 or more. | 1 |

`description` and `version` are for the reader, and every step that ships
carries them. The engine does not act on them.

A recipe may raise or lower `max_workers` for its own use, by writing it
under the step:

```yaml
  - detect_objects_fast:
      max_workers: 12
```

A recipe may **not** set `environment`; the engine refuses the recipe with a
message saying so. The step file owns its environment, because the imports
that need it are in the same file. A step that must run in two environments
is two step files.

## Give a step its own environment

Name the environment in `METADATA`:

```python
METADATA = {"environment": "ZMART--object_analysis--cellpose"}
```

Environment names follow **`ZMART--<workflow>--<step>`**: the workflow folder
the step lives in, and a short word for the environment. A workflow with one
environment calls it `main`; `object_analysis` has `cellpose` and
`classical`, because the deep-learning detector and the classical feature
extraction do not need the same packages, and a run that only needs the
second should not pay for the first.

The environment itself is made by the workflow's `environments/setup_env.py`,
which lists the packages and a few checks and hands the work to
`engine/conda_utils.py`:

```bash
python workflows/focus/environments/setup_env.py                   # makes ZMART--focus--main
python workflows/object_analysis/environments/setup_env.py --step cellpose
python workflows/object_analysis/environments/setup_env.py --step classical
```

The script takes these options:

| Option | What it does |
|---|---|
| `--step <name>` | Which of the workflow's environments to make. The default is the workflow's own default (`main` for `focus`). |
| `--python <version>` | The Python version to install, default 3.12. |
| `--gpu cu128\|cu124\|cu121\|mps\|cpu` | Which PyTorch build to install, for workflows that use torch. The default detects what the machine has. |
| `--check` | Run the checks on an environment that already exists, installing nothing. Use it after an update to see whether the environment still has what the workflow needs. |
| `--dry-run` | Print the commands without running them. |

Every environment is built from conda-forge only, and every package is
installed with pip inside it, as `setup_env.py` lists them. `clean_env.py` in
the same folder removes the workflow's environments again, all of them or one
with `--step`, and `--dry-run` only lists them.

When the engine starts a worker for a step, it runs `conda run -n <environment>`
with the environment's own interpreter, so the step sees that environment's
packages and no other. A step whose `METADATA` names the environment the
engine itself was started in is treated as having none: it runs in the
engine's own interpreter, which saves a process.

## Keep a model loaded

Loading a model takes seconds; using it takes milliseconds. A worker starts
once and stays running, and `state` is the dictionary it keeps for your step
between jobs. Load into it on the first job, and the model is still there on
the next:

```python
def run(pipeline_data, state, **params):
    if "model" not in state:
        from cellpose import models
        state["model"] = models.CellposeModel(gpu=params.get("gpu", True))
    masks, *_ = state["model"].eval(image)
    ...
```

`state` is per step and per worker. Two copies of the same step
(`max_workers: 2`) each load their own model, and two different steps in the
same environment do not see each other's `state`. A worker that has been idle
for the engine's `idle_timeout` (five minutes by default) is shut down, and
the next job starts a fresh one with an empty `state`.

## A step over a scope

A step given a `scope` in the recipe does not run per image. It waits until
the acquisition says that unit of the sample is complete, then runs once over
everything collected for it:

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

The scope is a word of your choosing. The workflows that ship use four
levels, narrowest first: **group**, **compartment**, **carrier**, and `all`
for the whole experiment. Each is a plain number at submission, so a
compartment can be a well of a plate, a region of a slide or part of a
cleared sample. A step over a wider scope collects the results of the step
over the narrower one: `compare_populations` receives one result per
compartment, not one per image.

Such a step receives a different `pipeline_data`:

| Key | What it is |
|---|---|
| `results` | A list: the result of every image (or every narrower unit) in this unit, in submission order. Each is the `pipeline_data` the previous phase returned. |
| `failures` | The jobs in this unit that failed, each with its step name and error. |
| `metadata["scope"]` | Which unit this is, for example `{"carrier": 1, "compartment": 3}`. |
| `metadata["scope_level"]` | The level, for example `"compartment"`. |
| `metadata["n_accumulated"]`, `metadata["n_failures"]` | How many of each. |

There is no `input` here. **Return a new dictionary** with your output;
copying the per-image results forward would only use memory. The result is
published to `engine.results()` with `_scope` and `_scope_level` added, so a
reader can tell which unit it describes.
`workflows/object_analysis/steps/summarise_population.py` is the worked
example.

## Parameters: the recipe, and overrides per submission

The parameters under a step in the recipe arrive as `**params`. Give every
parameter a default in the recipe, with a comment saying what it does; the
recipe is the record of what was asked for, and a reader should not need the
step file to understand it.

A step may also let a single submission override a recipe parameter, by
looking in `pipeline_data["input"]` first:

```python
def _setting(inp, params, key):
    return inp.get(key, params.get(key, None))
```

This is how `detect_objects` lets the operator page tune the detection on
one position without registering a recipe of its own: the recipe holds the
default, and a submission that carries `diameter` wins for that image only.
Whether a step allows this, and for which parameters, is the step's own
choice; say so in its docstring.

## What a result carries back

Before a result leaves the worker, the engine adds a record of where it ran,
under `pipeline_data["provenance"][<step name>]`:

| Key | What it is |
|---|---|
| `environment` | The conda environment's name. |
| `python` | The interpreter version. |
| `fingerprint` | A short hash of every package installed in the environment and its version. Two results with the same fingerprint ran on identical environments. |
| `packages` | The version of every package this worker has actually imported: torch and numpy, not the two hundred packages sitting unused. |

The recipe says what was asked for; `provenance` says what actually ran. A
step does not need to do anything for this. It is added only when the step
returns a dictionary, which it must anyway.

## The folder structure of a workflow

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

And the repository around it:

```text
ZMART-analysis/
├── engine/                     the engine; you rarely need to open it
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

A step that needs a shared helper adds `workflows/` to Python's search path
first, because a step file is loaded on its own, not as part of a package:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from shared.image_io import load_plane
```

Keeping one copy means a fix reaches every step that uses it.

A few files are called `__init__.py`. Python needs that exact name to treat a
folder as a package it can import from. `engine/__init__.py` is where
`from engine import Engine` comes from; the one in `workflows/shared/` only
marks its folder.

## Add a workflow

1. Copy an existing workflow folder and rename it. `focus` is the smallest.
2. Write the steps in `steps/`, one file each, as described above.
3. Name each step's environment `ZMART--<workflow>--<step>` in its
   `METADATA`, and list the packages in `environments/setup_env.py`. The
   script only lists packages and checks; creating the environment is
   `engine/conda_utils.py`'s job.
4. Write a recipe in `pipelines/`, with every parameter and a comment on each.
5. Write tests in `tests/`, and a short `README.md` that says what the
   workflow is for and how to choose its settings.

## Test a step

A step is an ordinary function, so a test calls it directly, with no engine
and no worker:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "steps"))
from double_it import run


def test_double_it():
    out = run({"input": {"n": 21}}, {})
    assert out["double_it"]["value"] == 42
```

Pass an empty dictionary as `state`, and the parameters as keyword arguments.
For a step over a scope, pass `{"results": [...], "failures": [],
"metadata": {"scope": {...}, "scope_level": "compartment"}}`.

Tests that need something the machine may not have mark themselves, so they
skip rather than fail:

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

`workflows/focus/tests/test_focus.py` shows the pattern: it scores a
synthetic z-stack directly, then, when `ZMART--focus--main` exists, once more
through the engine.

## Rules every step follows

- **One file, one `run`.** Helpers go below `run` in the same file, or in
  `workflows/shared/` when more than one workflow needs them.
- **Publish under your own name.** `pipeline_data[<step name>] = {...}`, and
  return `pipeline_data`. A step over a scope returns a new dictionary.
- **Imports that need the environment go inside `run`**, or below
  `METADATA`. The engine reads `METADATA` as text; the imports only run in
  the worker.
- **`METADATA` is a literal.** No function calls, no variables.
- **Every parameter has a default in the recipe**, with a comment.
- **Fail loudly.** Raise on an input you cannot handle; the engine reports
  it for that job and carries on with the others. Do not return a half
  result quietly.
- **Nothing here moves the stage.** A step reads images and returns numbers.
  What to do about them is the workflow's decision, outside the engine.
