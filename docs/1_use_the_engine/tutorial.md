# Use the engine, step by step

This is a walk-through for someone who would like to run an image analysis
with the ZMART Analysis engine, and has not used it before. It explains the
idea, takes you through a first run on a z-stack made on your own computer,
and then shows a step that waits for a whole group of images. The
[README](README.md) of this part is the complete documentation of every
call.

Every snippet here was checked against `engine/engine.py`,
`workflows/focus/steps/score_focus.py` and the tests in
`workflows/focus/tests/test_focus.py` and `tests/test_engine.py`.

## The idea

An analysis is a *recipe*, a YAML file that lists the steps in order with
their parameters. You register the recipe with the engine, submit images
to it, and collect the results. Each step runs in a *worker*, a Python
process inside the conda environment that step asked for, which starts
once and stays running.

```
your script ──► Engine ──► worker (conda env) ──► step
            ◄── results ◄──
```

The recipe we use first is `focus`, which ships with the engine. It scores
the sharpness of every plane in a z-stack and reports the height where the
sharpness peaks. It needs no GPU and no model, so it runs on a laptop.

## Before you start

- The engine installed as in [Install it](../../README.md#install-it),
  with its `zmart-analysis` conda environment active.
- The `focus` workflow's own environment, made once:

  ```
  python workflows/focus/environments/setup_env.py
  ```

Open Python in the root folder of the repository, by typing `python` in a
terminal, or open a Jupyter notebook there, and type the lines below one at
a time.

## Step 1: make a z-stack

A real stack comes from the microscope. For now we make one: a noisy image,
sharp at plane 4 and blurred more and more either side of it. This is the
same stack the `focus` tests use.

```python
import numpy as np
import tifffile

def blurred(image, radius):
    out = image.astype(np.float64)
    for _ in range(radius):
        out = (np.roll(out, 1, 0) + out + np.roll(out, -1, 0)) / 3.0
        out = (np.roll(out, 1, 1) + out + np.roll(out, -1, 1)) / 3.0
    return out

sharp = np.random.default_rng(0).integers(0, 4096, size=(64, 64)).astype(np.float64)
planes = [blurred(sharp, abs(z - 4)) for z in range(9)]
tifffile.imwrite("stack.tiff", np.stack(planes).astype(np.uint16), metadata={"axes": "ZYX"})
```

Nine planes, 64 by 64 pixels, in one file that says its first axis is z.
`numpy` and `tifffile` were installed with the engine's test extras.

## Step 2: make an engine

```python
from engine import Engine

engine = Engine(idle_timeout=None)
```

`idle_timeout=None` keeps every worker alive for as long as the engine
lives. The default stops a worker after five minutes of idleness, to free
its memory; during an acquisition, where the gaps between images can be
long, you do not want that.

## Step 3: register the recipe

```python
engine.register("focus", "workflows/focus/pipelines/focus.yaml")
```

The engine reads the recipe, finds the step file `score_focus.py` next to
it, and reads the `METADATA` at its top. That tells it the step runs in the
conda environment `ZMART--focus--main`. No step code has run yet.

Open `workflows/focus/pipelines/focus.yaml` and read it. It has one step
with a handful of parameters, and a comment that says what to submit with
each job: `image_path` or `image_paths`, and `z_um`, the height each plane
was taken at.

## Step 4: submit the stack

```python
z_um = [100.0 + 2.0 * z for z in range(9)]
engine.submit("focus", {"image_path": "stack.tiff", "z_um": z_um})
```

`submit` returns at once. Behind it, the engine starts a worker in
`ZMART--focus--main`, which takes a few seconds the first time, and hands
it the stack. On a microscope this is the point: the acquisition goes on
while the analysis runs.

`z_um` is the height of each plane, so the answer can be a height rather
than a plane number. We took the planes 2 µm apart from 100 µm.

## Step 5: read the result

```python
import time

results = engine.results("focus")
while not results:
    time.sleep(0.5)
    results = engine.results("focus")

focus = results[0]["score_focus"]
print(focus["found"], focus["peak_index"], focus["peak_z_um"])
```
```
True 4.0 108.0
```

`results` gives back everything finished since you last asked, and each
result is a dictionary with the step's output under the step's name. The
peak is on plane 4, at 108 µm, as we built it. `focus["metrics"]` holds the
sharpness curve of every metric, so you can plot and compare them:

```python
print(focus["metrics"]["brenner"]["scores"])
```

Two more keys are worth a look. `results[0]["input"]` is what you
submitted, and `results[0]["provenance"]["score_focus"]` says in which
environment the step actually ran, on which Python, with which package
versions:

```python
print(results[0]["provenance"]["score_focus"]["environment"])
```
```
ZMART--focus--main
```

## Step 6: submit several at once

```python
for i in range(5):
    engine.submit("focus", {"image_path": "stack.tiff", "z_um": z_um})

print(engine.status("focus"))
```
```
{'pending': 5, 'running': 0, 'completed': 1, 'failed': 0, 'failures': []}
```

`status` shows what is waiting, running and done; asked this quickly, the
five are still waiting. The worker takes them one after another, because
`score_focus` has `max_workers: 1` in its `METADATA`, and one at a time is
enough for it. Wait a moment and collect them:

```python
time.sleep(3)
print(len(engine.results("focus")))
```
```
5
```

The first result was taken off the queue in step 5, so these are the five
new ones.

## Step 7: a step that waits for a group

Some analysis cannot run per image. A summary over a well needs every
image of the well. In a recipe, such a step has a `scope`, and it waits
until a submit says the unit is complete.

We write two tiny steps for this and keep them in a folder of their own.
They name no environment, so they run in the engine's own Python.

```python
from pathlib import Path

demo = Path("demo"); (demo / "steps").mkdir(parents=True, exist_ok=True)

(demo / "steps" / "measure.py").write_text('''
def run(pipeline_data, state, **params):
    pipeline_data["measure"] = {"value": pipeline_data["input"]["value"]}
    return pipeline_data
''')

(demo / "steps" / "summarise.py").write_text('''
def run(pipeline_data, state, **params):
    values = [r["measure"]["value"] for r in pipeline_data["results"]]
    pipeline_data["summarise"] = {"n": len(values), "mean": sum(values) / len(values)}
    return pipeline_data
''')

(demo / "group.yaml").write_text('''
metadata:
  functions_dir: "steps"

group_demo:
  - measure:
  - summarise:
      scope: group
''')
```

`measure` runs on every image. `summarise` has `scope: group`, so it runs
once per group, and what it receives is different: `pipeline_data["results"]`
is the list of every `measure` result of that group, in the order they
were submitted.

```python
engine.register("group_demo", "demo/group.yaml")

engine.submit("group_demo", {"value": 10}, scope={"group": "A"})
engine.submit("group_demo", {"value": 20}, scope={"group": "A"})
engine.submit("group_demo", {"value": 30}, scope={"group": "A"}, complete="group")

time.sleep(2)
for r in engine.results("group_demo"):
    print(r["_phase"], r["_scope_level"], r.get("summarise"))
```
```
0 None None
0 None None
0 None None
1 group {'n': 3, 'mean': 20.0}
```

Three per-image results come first, each with `_phase` 0. Then one result
with `_phase` 1, for the group: `summarise` ran once, over all three. The
third submit said `complete="group"`, and that is what started it. The
engine never guesses when a group is done; the acquisition says so.

## Step 8: shut down

```python
engine.shutdown()
```

The engine stops accepting work, lets the queued jobs finish, and stops
every worker. In a script, the `with` form does this for you:

```python
with Engine(idle_timeout=None) as engine:
    engine.register("focus", "workflows/focus/pipelines/focus.yaml")
    ...
```

## Step 9: when something goes wrong

A step that fails does not stop the engine. Submit a stack that does not
exist:

```python
with Engine() as engine:
    engine.register("focus", "workflows/focus/pipelines/focus.yaml")
    engine.submit("focus", {"image_path": "missing.tiff"})
    time.sleep(3)
    print(engine.status("focus")["failures"])
```
```
[{'scope': {}, 'step': 'score_focus',
  'error': "FileNotFoundError: [Errno 2] No such file or directory: '.../missing.tiff'"}]
```

The failure is counted and its message kept; the other jobs carry on. If
the worker could not start at all, the message names the environment,
and the fix is to run that workflow's `environments/setup_env.py`.

## Points to be aware of

- **`submit` never waits.** Results arrive in `results` when they are done,
  and a result taken from `results` is gone from it. Keep what you need.
- **Keep the workers.** `Engine(idle_timeout=None)` for an acquisition;
  the default of five minutes is for short scripts.
- **A scoped recipe keeps results in memory until its unit is closed.** Say
  `complete=` on the last image of every unit, or the memory grows.
- **The recipe is the record.** Change a parameter in the YAML, not in the
  step, so the file still says what was done. The `provenance` in each
  result says what actually ran.
- **What to submit is up to the recipe.** Each recipe's comment at the top,
  and each step's docstring, say what `data` must contain.

## Where to go next

- The [README](README.md) of this part: every call, every argument, and
  the full rules for scopes and workers.
- Part 2, [implement an analysis step](../2_implement_an_analysis_step/README.md):
  the one function a step needs, and how to give it its own environment.
- Part 3, [the workflows we use](../3_workflows_we_use/README.md): focus,
  object analysis and driver configuration, as they ship.

---

MIT license. Thom de Hoog, Center for Microscopy and Image Analysis (ZMB),
University of Zurich. thom.dehoog@zmb.uzh.ch, thomdehoog@gmail.com.
