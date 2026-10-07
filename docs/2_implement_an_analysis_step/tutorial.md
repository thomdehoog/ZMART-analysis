# Implement an analysis step, step by step

This is a walk-through for someone who would like to add their own analysis
to ZMART Analysis, with no experience of the engine. It writes a tiny step,
runs it through the engine, gives it its own conda environment, and then adds
a second step that waits for a group of images before it runs. The
[README](README.md) is the complete documentation: everything a step
receives and returns, every `METADATA` key, and the rules behind them.

## The idea

A step is one Python file with one function, `run`. The engine hands it a
dictionary with the image, the step adds its result to the dictionary under
its own name, and returns it. A recipe, a small YAML file, lists which steps
run in which order. That is the whole contract.

```
  recipe (YAML) ──► engine ──► worker in the step's environment ──► run(...)
```

Everything in this tutorial runs on a laptop, with no microscope and no GPU.

## Before you start

- ZMART Analysis installed, as in *Install it* in the
  [main README](../../README.md): the repository cloned, a conda environment
  `zmart-analysis` made and activated, and `pip install -e ".[test]"` done
  in it.
- A command window with that environment active.

The steps below make a new workflow folder, `workflows/intensity/`. Make it
with the same four folders every workflow has:

```bash
mkdir -p workflows/intensity/pipelines workflows/intensity/steps \
         workflows/intensity/environments workflows/intensity/tests
```

## Step 1: write the step

Create `workflows/intensity/steps/mean_intensity.py`:

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

Three things to notice. The image comes in under `pipeline_data["input"]`,
which is exactly what will be passed to `engine.submit`. The result goes
under `pipeline_data["mean_intensity"]`, the step's own name. And the step
returns the whole dictionary, so the next step sees everything this one saw.

## Step 2: call it directly

A step is a plain function. Before involving the engine, call it:

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

The second argument, `{}`, is `state`: a dictionary the step may keep things
in between jobs. This step does not need it. Any parameters would follow as
keyword arguments.

This is also how a test calls a step. Put the same lines, with an `assert`,
in `workflows/intensity/tests/test_mean_intensity.py`, and `pytest
workflows/intensity` runs it.

## Step 3: write the recipe

Create `workflows/intensity/pipelines/intensity.yaml`:

```yaml
# The mean and the brightest pixel of every image.

metadata:
  purpose: "Mean intensity per image"
  version: "1.0"
  functions_dir: "../steps"

intensity:
  - mean_intensity:
```

The first key after `metadata` names the workflow. Under it, the steps in
order; each is a file in `functions_dir`, which is relative to the recipe.
`mean_intensity:` with nothing under it means no parameters.

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
image_0 {'mean': 2041.9, 'max': 4095.0}
image_1 {'mean': 2047.1, 'max': 4094.0}
image_2 {'mean': 2063.5, 'max': 4095.0}
```

`register` reads the recipe and the step file's `METADATA` (there is none
yet, so the step runs in the environment you are in). `submit` returns at
once; the work happens in a worker in the background. `shutdown(wait=True)`
waits for every job, and `results` hands back one dictionary per job. Every
`Engine` call is in [Use the engine](../1_use_the_engine/README.md).

Look at one result more closely:

```python
print(result["provenance"]["mean_intensity"])
```

```
{'environment': 'zmart-analysis', 'python': '3.12.4', 'fingerprint': '…', 'packages': {'numpy': '2.1.0'}}
```

The engine added where the step ran and which packages it imported. The
recipe says what was asked for; this says what actually ran.

## Step 5: add a parameter

Suppose the brightest pixel should be a percentile rather than the maximum,
so one hot pixel cannot decide it. Give the step a parameter:

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

And write it in the recipe, with its default and a comment:

```yaml
intensity:
  - mean_intensity:
      percentile: 99.9    # the brightest this share of pixels; 100 is the true maximum
```

Parameters under a step arrive as keyword arguments. Writing the default in
the recipe, not only in the code, is what makes the recipe a complete record
of what was done. Someone who reads the YAML a year from now should not have
to open the step file.

## Step 6: give it its own environment

So far the step runs in the environment you started from. Suppose it needed a
package that conflicts with something else you use. Then it gets an
environment of its own.

First, name it in the step file, above `run`:

```python
METADATA = {
    "description": "Mean intensity and a brightness percentile of one image",
    "version": "1.0",
    "environment": "ZMART--intensity--main",
    "max_workers": 4,
}
```

The name follows `ZMART--<workflow>--<step>`; with one environment, call it
`main`. `max_workers: 4` lets four copies of this step run at once, which is
fine for work on the processor. The engine reads this dictionary as text,
without running the file, so it must be a plain literal.

Then make the environment. Create
`workflows/intensity/environments/setup_env.py`:

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

It makes `ZMART--intensity--main` from conda-forge, installs the listed
packages with pip, and runs the diagnostic inside it. Now run Step 4 again.
Nothing in the Python changes, but the provenance does:

```
{'environment': 'ZMART--intensity--main', 'python': '3.12.4', ...}
```

The engine started a worker inside the new environment and sent the images
there. The worker stays running between images, so a model loaded on the
first image, kept in `state`, would still be there for the second. (This
step has nothing to load; the README shows the pattern.)

## Step 7: a step over a group

Per-image numbers are rarely the end. Suppose the question is how the
intensity varies across a group of images, say the tiles of one region. That
answer can only be given once the whole group is in.

Create `workflows/intensity/steps/summarise_group.py`:

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

Two differences from a per-image step. It receives `pipeline_data["results"]`,
a list with one entry per image in the group, each being what
`mean_intensity` returned for it. And it returns a *new* dictionary, not
`pipeline_data`: carrying every image's result forward would only use memory.

Add it to the recipe with a `scope`:

```yaml
intensity:
  - mean_intensity:
      percentile: 99.9
  - summarise_group:
      scope: group
```

Then submit images with a `scope`, and tell the engine when a group is
complete:

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
{'group': 1} 4 9.3
{'group': 2} 4 11.7
```

`mean_intensity` ran on every image the moment it was submitted. `summarise_group`
ran twice, once per group, each time after the `complete="group"` signal,
over that group's four results. The engine never guesses when a group is
done; the acquisition knows, and says so with `complete`.

## Step 8: clean up

```bash
python workflows/intensity/environments/clean_env.py
```

removes `ZMART--intensity--main`. Delete `workflows/intensity/` when you are
done with it, or keep it as the start of a real workflow: add a `README.md`
saying what it is for, and it has everything the shipped workflows have.

## When something goes wrong

- **`Step 'x' sets 'environment' in the pipeline YAML`** at `register`. The
  environment belongs in the step file's `METADATA`, not the recipe. Move it.
- **A job fails and the others carry on.** `engine.status("intensity")`
  lists the failures with the step name and the error message, and the
  traceback from inside the worker. The step raised; fix it and resubmit.
- **`WorkerSpawnError` naming an environment.** The environment in
  `METADATA` does not exist, or conda cannot be found. Run the workflow's
  `setup_env.py`, or `setup_env.py --check` to see what is missing.
- **The step runs, but with old code.** A worker stays running and keeps the
  module it imported. Call `engine.shutdown()` and make a new `Engine` after
  editing a step.
- **The scoped step never runs.** No `complete="group"` was sent, or it was
  sent with a different `scope` value than the images. The signal and the
  images must name the same group.

## Points to be aware of

- `METADATA` is read as text. A function call or a variable in it is an
  error at `register`.
- `state` belongs to one step in one worker. Two copies (`max_workers: 2`)
  each have their own.
- A worker idle for five minutes is shut down and its `state` with it; the
  next job starts a fresh one.
- The step's result must be a dictionary. Anything else fails the job.

## Where to go next

- The [README](README.md) of this part: every key of `pipeline_data` and
  `METADATA`, overriding parameters per submission, and the rules every
  step follows.
- [Use the engine](../1_use_the_engine/README.md): `register`, `submit`,
  `status`, `results`, scopes and priorities in full.
- `workflows/focus/steps/score_focus.py`: a real step of modest size, with
  its own environment and a test.
- `workflows/object_analysis/steps/summarise_population.py`: a real step
  over a scope.
