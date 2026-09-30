# Writing a step

A step is one Python file with one function, `run`. This page shows the
smallest step, then everything a step can add.

## The smallest step

`steps/double_it.py`:

```python
def run(pipeline_data, state, **params):
    n = pipeline_data["input"]["n"]
    pipeline_data["double_it"] = {"value": n * 2}
    return pipeline_data
```

And a recipe that uses it, `pipelines/hello.yaml`:

```yaml
metadata:
  functions_dir: "../steps"

hello:
  - double_it:
```

## What `run` receives and returns

| Argument | What it is |
|---|---|
| `pipeline_data` | A dictionary. `pipeline_data["input"]` is what was submitted; each earlier step has added its output under its own name. |
| `state` | A dictionary that survives between jobs in the same worker. Empty on the first job. |
| `**params` | The parameters written under this step in the recipe. |

Return `pipeline_data` with your output added under your step's name. That
is how the next step, and the person reading the results, finds it. A step
that raises an error fails only its own job, and the error message appears
in `engine.status()`.

## Keeping a model loaded

Loading a model is slow; using it is fast. Load it once, into `state`:

```python
def run(pipeline_data, state, **params):
    if "model" not in state:
        from cellpose import models
        state["model"] = models.CellposeModel(gpu=params.get("gpu", True))
    masks, *_ = state["model"].eval(image)
    ...
```

The first tile pays for the loading; every tile after it does not.

## Giving a step its own environment

Put a `METADATA` dictionary at the top of the file:

```python
METADATA = {
    "environment": "ZMART--object_analysis--cellpose",   # a conda environment
    "max_workers": 1,                                     # copies at once
}
```

- **`environment`** is the conda environment the step runs in. Leave it out
  to run in the environment you started from. The engine reads this without
  running the file, so the heavy imports only ever happen inside the right
  environment.
- **`max_workers`** is how many copies may run at the same time. Keep it at
  1 for a GPU model; raise it for work on the processor. A recipe may raise
  or lower it for its own use.

The environment itself is made by the workflow's
`environments/setup_env.py`. Environment names follow
`ZMART--<workflow>--<purpose>`.

## A step that runs over a unit

A step given a `scope` in the recipe runs once per completed unit, and
receives a different `pipeline_data`:

| Key | What it is |
|---|---|
| `results` | The results of every tile (or narrower unit) in this unit, in submission order. |
| `failures` | The jobs in this unit that failed, with their errors. |
| `metadata["scope"]` | Which unit this is, for example `{"carrier": 1, "compartment": 3}`. |
| `metadata["scope_level"]` | The level, for example `"compartment"`. |

Return a new dictionary with your output. There is no `input` here, and
copying the tile results forward would only use memory.
`summarise_population.py` in object analysis is a worked example.

## Testing a step

A step is an ordinary function, so a test can call it directly:

```python
from double_it import run

def test_double_it():
    out = run({"input": {"n": 21}}, {})
    assert out["double_it"]["value"] == 42
```

Each workflow keeps its tests in its own `tests/` folder. See
[Folder structure](folder-structure.md).
