# 2. Implement an analysis step

This page shows how to write a step for the ZMART Analysis engine. For a
guided example, open the [tutorial notebook](tutorial.ipynb). How to run a
pipeline is in [part 1](../1_use_the_engine/README.md).

## The smallest step

A step is one Python file with one function, `run`. This is
`steps/double_it.py`:

```python
def run(pipeline_data, state, **params):
    n = pipeline_data["input"]["n"]
    pipeline_data["double_it"] = {"value": n * 2}
    return pipeline_data
```

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

The last step of a workflow may use the workflow's name as its key instead,
when that is the result everyone will ask for. `build_object_table` puts
the object table under `object_analysis`.

If your step raises an error, only its own job fails. On the job board,
`engine.status(name)`, the job is `failed`, with the step and the error.

`pipeline_data["metadata"]` holds:

| Key | What it is |
|---|---|
| `workflow_name` | The name of the pipeline's step list, for example `focus`. |
| `yaml_filename` | The pipeline's file name. |
| `steps` | The names of the steps that run with this one, in order. |
| `scope` | The `scope` given at submit, for example `{"carrier": 1, "compartment": 3}`. |
| `submission_idx` | The job's number in submission order, counted from zero. |
| `datetime` | When the job started, as `YYYYMMDD-HHMMSS`. |
| `verbose` | The pipeline's `verbose` setting. Print progress when it is above `0`, and nothing when it is `0`. |

## METADATA

A step file may hold a `METADATA` dictionary. It tells the engine how to
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
| `environment` | The conda environment the step runs in. | The environment the engine itself runs in. |
| `max_workers` | How many copies of the step may run at once. | `1` |
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

A scoped step receives a different `pipeline_data`. There is no `input`:

| Key | What it is |
|---|---|
| `results` | One entry per image of the unit, in submission order. Each entry is what the per-image steps returned. For a wider scope, there is one entry per narrower unit instead: a carrier step receives one result per compartment. |
| `failures` | What failed in this unit. Each failure has `scope`, `step`, `error`, `phase` and `submission_idx`. |
| `metadata["unit"]` | Which unit this is, widest level first, for example `{"carrier": 1, "compartment": 3}`. It is `{}` for a step over everything. |
| `metadata["scope_level"]` | The level, for example `"compartment"`. |
| `metadata["scope"]` | The `scope` of the image that completed the unit. |
| `metadata["n_accumulated"]`, `metadata["n_failures"]` | How many results and how many failures. |

A failure with `"step": "engine"` did not come from a step. It is the
engine's own note: an image of a narrower unit that was never completed.
Completing a level completes the narrower units inside it, so this happens
only when the pipeline cannot tell that a level is narrower: list your levels
under `levels`.

**Return a new dictionary with your output.** Do not copy the per-image
results into it; that only uses memory. The engine adds `metadata` and
`lineage` to your result afterwards. A later step with the same scope
receives what you returned.

`workflows/object_analysis/steps/summarise_population.py` is a worked
example.

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

`detect_objects` does this, so an operator can tune detection for one
position. The pipeline holds the default, and a job that carries `diameter`
uses its own value for that image only. Say in the docstring which
parameters can be overridden this way.

## The folder of a workflow

```text
workflows/object_analysis/
├── README.md              what the workflow does and how to choose its settings
├── pipelines/             the pipelines (YAML)
├── steps/                 one Python file per step
├── parts/                 optional: code the steps share, so a step stays short
├── environments/
│   ├── setup_env.py       makes the environments
│   └── clean_env.py       removes them again
└── tests/
```

Code that several workflows use lives in `workflows/shared/`.

The engine loads a step file on its own, not as part of a package. To
import from `shared/` or `parts/`, a step first adds the `workflows/` folder
to Python's search path:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from shared.image_io import load_plane
from object_analysis.parts.masks import filter_masks_by_area
```

To add a workflow, copy `focus`. It is the smallest one. Then:

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

To test a scoped step, pass it `{"results": [...], "failures": [],
"metadata": {"unit": {...}, "scope_level": "compartment"}}`.

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

`workflows/focus/tests/test_focus.py` shows the pattern. It scores a
made-up z-stack directly and then, if `ZMART--focus--main` exists, once
more through the engine.

When you try an edited step through the engine, make a new `Engine` first.
A running worker keeps the code it loaded.
