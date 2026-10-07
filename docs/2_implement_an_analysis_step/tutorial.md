# Implement an analysis step, step by step

Write a tiny step, run it through the engine, give it its own conda
environment, then add a second step that waits for a group of images. The
[README](README.md) of this part documents everything a step receives and
returns.

A step is one Python file with one function, `run`. The engine hands it a
dictionary with the image, the step adds its result under its own name, and
returns it. A recipe lists which steps run in which order. That is the
whole contract. Everything here runs on a laptop, no GPU.

## Before you start

- ZMART Analysis installed as in [Install it](../../README.md#install-it).
- `numpy`: `pip install numpy`.

Make the new workflow folder, with the four folders every workflow has:

```bash
mkdir -p workflows/intensity/pipelines workflows/intensity/steps \
         workflows/intensity/environments workflows/intensity/tests
```

## Step 1: write the step

`workflows/intensity/steps/mean_intensity.py`:

```python
"""mean_intensity -- the mean and the brightest pixel of one image."""

import numpy as np


def run(pipeline_data, state, **params):
    image = np.asarray(pipeline_data["input"]["image"], dtype=float)
    pipeline_data["mean_intensity"] = {
        "mean": float(image.mean()),
        "max": float(image.max()),
    }
    return pipeline_data
```

The image comes in under `pipeline_data["input"]`, exactly what will be
passed to `engine.submit`. The result goes under the step's own name. The
step returns the whole dictionary, so the next step sees everything.

## Step 2: call it directly

A step is a plain function. Before the engine, call it:

```python
import sys
import numpy as np

sys.path.insert(0, "workflows/intensity/steps")
from mean_intensity import run

out = run({"input": {"image": np.arange(16).reshape(4, 4)}}, {})
print(out["mean_intensity"])
```
```
{'mean': 7.5, 'max': 15.0}
```

The `{}` is `state`, a dictionary the step may keep things in between
jobs. This is also how a test calls a step: the same lines with an
`assert`, in `workflows/intensity/tests/test_mean_intensity.py`.

## Step 3: write the recipe

`workflows/intensity/pipelines/intensity.yaml`:

```yaml
# The mean and the brightest pixel of every image.

metadata:
  purpose: "Mean intensity per image"
  version: "1.0"
  functions_dir: "../steps"

intensity:
  - mean_intensity:
```

The first key after `metadata` names the workflow; under it, the steps in
order. `mean_intensity:` with nothing under it means no parameters.

## Step 4: run it through the engine

```python
import numpy as np
from engine import Engine

engine = Engine()
engine.register("intensity", "workflows/intensity/pipelines/intensity.yaml")

rng = np.random.default_rng(0)
for i in range(3):
    engine.submit("intensity", {"image": rng.integers(0, 4096, (64, 64)), "name": f"image_{i}"})

engine.shutdown(wait=True)
for result in engine.results("intensity"):
    print(result["input"]["name"], result["mean_intensity"])
```
```
image_0 {'mean': 2062.80224609375, 'max': 4095.0}
image_1 {'mean': 2022.114501953125, 'max': 4095.0}
image_2 {'mean': 2060.589599609375, 'max': 4095.0}
```

There is no `METADATA` yet, so the step runs in the environment you are
in. `shutdown(wait=True)` waits for every job. Look at one result:

```python
print(result["provenance"]["mean_intensity"])
```
```
{'environment': 'zmart-microscopy', 'python': '3.11.15', 'fingerprint': 'ced179088d40e21f', 'packages': {'numpy': '2.4.6', 'setuptools': '83.0.0'}}
```

The engine added where the step ran and which packages it imported. Your
environment name and versions will differ; that is the point.

## Step 5: add a parameter

Make the brightest pixel a percentile, so one hot pixel cannot decide it:

```python
def run(pipeline_data, state, **params):
    percentile = float(params.get("percentile", 100))
    image = np.asarray(pipeline_data["input"]["image"], dtype=float)
    pipeline_data["mean_intensity"] = {
        "mean": float(image.mean()),
        "max": float(np.percentile(image, percentile)),
        "percentile": percentile,
    }
    return pipeline_data
```

And in the recipe, with its default and a comment:

```yaml
intensity:
  - mean_intensity:
      percentile: 99.9    # the brightest this share of pixels; 100 is the true maximum
```

Parameters under a step arrive as keyword arguments. The default in the
recipe is what makes the recipe a complete record of what was done.

## Step 6: give it its own environment

Name it in the step file, above `run`:

```python
METADATA = {
    "description": "Mean intensity and a brightness percentile of one image",
    "version": "1.0",
    "environment": "ZMART--intensity--main",
    "max_workers": 4,
}
```

`ZMART--<workflow>--<step>`; with one environment, call it `main`.
`max_workers: 4` lets four copies run at once. The engine reads this as
text, so it must be a plain literal.

Then `workflows/intensity/environments/setup_env.py`:

```python
"""Create the conda environment for the intensity workflow."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "engine"))
from conda_utils import setup_workflow_env  # noqa: E402

if __name__ == "__main__":
    setup_workflow_env(
        workflow="intensity",
        pip_packages=["numpy"],
        diagnostics=[("numpy imports", "import numpy; print(numpy.__version__)")],
        install_torch=False,
    )
```

and `clean_env.py` beside it:

```python
"""Remove the conda environments of the intensity workflow."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "engine"))
from conda_utils import clean_workflow_envs  # noqa: E402

if __name__ == "__main__":
    clean_workflow_envs(workflow="intensity")
```

Run the setup once:

```bash
python workflows/intensity/environments/setup_env.py
```

Now run Step 4 again. Nothing in the Python changes, but the output does:

```
image_1 {'mean': 2022.114501953125, 'max': 4093.8100000000013, 'percentile': 99.9}
image_2 {'mean': 2060.589599609375, 'max': 4092.0, 'percentile': 99.9}
image_0 {'mean': 2062.80224609375, 'max': 4092.9050000000007, 'percentile': 99.9}
{'environment': 'ZMART--intensity--main', 'python': '3.12.15', 'fingerprint': '0eb1bb28efc1a380', 'packages': {'numpy': '2.5.3', 'setuptools': '84.0.0'}}
```

The `max` is now the 99.9th percentile. The results come back in a
different order, because `max_workers: 4` ran the three images at once;
`input["name"]` says which is which. And the provenance names the new
environment.

The worker in the new environment stays running between images; a model
loaded on the first image and kept in `state` would still be there for the
second.

## Step 7: a step over a group

How does the intensity vary across a group of images? That can only be
answered once the whole group is in.

`workflows/intensity/steps/summarise_group.py`:

```python
"""summarise_group -- the spread of mean intensity over one completed group."""

import numpy as np

METADATA = {
    "description": "Mean and spread of intensity over a group of images",
    "version": "1.0",
    "environment": "ZMART--intensity--main",
}


def run(pipeline_data, state, **params):
    means = [r["mean_intensity"]["mean"] for r in pipeline_data["results"]]
    return {
        "group": pipeline_data["metadata"]["scope"],
        "n_images": len(means),
        "n_failed": len(pipeline_data["failures"]),
        "mean": float(np.mean(means)) if means else None,
        "spread": float(np.std(means)) if means else None,
    }
```

Two differences from a per-image step: it receives
`pipeline_data["results"]`, one entry per image in the group, and it
returns a *new* dictionary, not `pipeline_data`.

Add it to the recipe with a `scope`:

```yaml
intensity:
  - mean_intensity:
      percentile: 99.9
  - summarise_group:
      scope: group
```

Submit images with a `scope`, and say when a group is complete:

```python
engine = Engine()
engine.register("intensity", "workflows/intensity/pipelines/intensity.yaml")

for group in (1, 2):
    for i in range(4):
        last = i == 3
        engine.submit(
            "intensity",
            {"image": rng.integers(0, 4096, (64, 64)), "name": f"g{group}_image_{i}"},
            scope={"group": group},
            complete="group" if last else None,
        )

engine.shutdown(wait=True)
for result in engine.results("intensity"):
    if result.get("_scope_level") == "group":
        print(result["group"], result["n_images"], round(result["spread"], 1))
```
```
{'group': 2} 4 14.6
{'group': 1} 4 18.6
```

`mean_intensity` ran on every image as it was submitted. `summarise_group`
ran once per group, after each `complete="group"`. The engine never
guesses when a group is done; the acquisition says so.

## Step 8: clean up

```bash
python workflows/intensity/environments/clean_env.py
```

removes `ZMART--intensity--main`. Delete `workflows/intensity/`, or keep it
as the start of a real workflow.

## When something goes wrong

- **`Step 'x' sets 'environment' in the pipeline YAML`** at `register`:
  the environment belongs in the step's `METADATA`, not the recipe.
- **A job fails, the others carry on.** `engine.status("intensity")` lists
  the failures with the step, the message and the worker's traceback.
- **`WorkerSpawnError` naming an environment.** It does not exist, or
  conda cannot be found. Run the workflow's `setup_env.py`, or
  `setup_env.py --check`.
- **The step runs with old code.** A worker keeps the module it imported.
  `engine.shutdown()` and make a new `Engine` after editing a step.
- **The scoped step never runs.** No `complete="group"` was sent, or with
  a different `scope` value than the images.

## Where to go next

- The [README](README.md) of this part: every key of `pipeline_data` and
  `METADATA`, overrides per submission, provenance, tests.
- [Use the engine](../1_use_the_engine/README.md): every `Engine` call.
- `workflows/focus/steps/score_focus.py`: a real step with its own
  environment and a test.
- `workflows/object_analysis/steps/summarise_population.py`: a real step
  over a scope.

---

MIT license. Thom de Hoog, Center for Microscopy and Image Analysis (ZMB),
University of Zurich. thom.dehoog@zmb.uzh.ch, thomdehoog@gmail.com.
