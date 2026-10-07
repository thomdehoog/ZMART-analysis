# Use the engine, step by step

A first run with the ZMART Analysis engine, on a z-stack made on your own
computer, followed by a step that waits for a whole group of images. The
[README](README.md) of this part documents every call.

We use the `focus` workflow, which ships with the engine: it scores the
sharpness of every plane in a z-stack and reports the height where it
peaks. No GPU, no model, so it runs on a laptop.

## Before you start

- The engine installed as in [Install it](../../README.md#install-it),
  with its `zmart-analysis` conda environment active.
- The `focus` workflow's own environment, made once:

  ```
  python workflows/focus/environments/setup_env.py
  ```

Open Python in the root folder of the repository and type the lines below
one at a time.

## Step 1: make a z-stack

A noisy image, sharp at plane 4 and blurred more and more either side of it.
This is the stack the `focus` tests use.

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

## Step 2: make an engine

```python
from engine import Engine

engine = Engine(idle_timeout=None)
```

`idle_timeout=None` keeps every worker alive as long as the engine lives.
The default stops a worker after five minutes idle; during an acquisition
the gaps between images can be longer than that.

## Step 3: register the recipe

```python
engine.register("focus", "workflows/focus/pipelines/focus.yaml")
```

The engine reads the recipe, finds `score_focus.py` next to it, and reads
the `METADATA` at its top: the step runs in `ZMART--focus--main`. No step
code has run yet. Open the recipe and read it; its comment says what to
submit with each job.

## Step 4: submit the stack

```python
z_um = [100.0 + 2.0 * z for z in range(9)]
engine.submit("focus", {"image_path": "stack.tiff", "z_um": z_um})
```

`submit` returns at once. Behind it the engine starts a worker in
`ZMART--focus--main`, a few seconds the first time, and hands it the stack.
`z_um` is the height of each plane, so the answer is a height, not a plane
number.

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

The peak is on plane 4, at 108 µm, as built. `focus["metrics"]` holds the
sharpness curve of every metric. `results[0]["input"]` is what you
submitted, and `results[0]["provenance"]["score_focus"]` says where the
step actually ran:

```python
print(results[0]["provenance"]["score_focus"]["environment"])
```
```
ZMART--focus--main
```

## Step 6: submit several

```python
for i in range(5):
    engine.submit("focus", {"image_path": "stack.tiff", "z_um": z_um})

print(engine.status("focus"))
```
```
{'pending': 5, 'running': 0, 'completed': 1, 'failed': 0, 'failures': []}
```

The worker takes them one after another, because `score_focus` has
`max_workers: 1`. Wait a moment and collect:

```python
time.sleep(3)
print(len(engine.results("focus")))
```
```
5
```

## Step 7: a step that waits for a group

Some analysis cannot run per image: a summary over a well needs every image
of the well. Such a step has a `scope` in the recipe and waits until a
submit says the unit is complete.

Two tiny steps, in a folder of their own. They name no environment, so they
run in the engine's own Python.

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

`measure` runs on every image. `summarise` runs once per group and
receives `pipeline_data["results"]`, the `measure` results of that group.

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

Three per-image results, then one for the group. The third submit said
`complete="group"`, and that started it. The engine never guesses when a
group is done; the acquisition says so.

## Step 8: shut down

```python
engine.shutdown()
```

In a script, the `with` form does this for you:

```python
with Engine(idle_timeout=None) as engine:
    engine.register("focus", "workflows/focus/pipelines/focus.yaml")
    ...
```

## Step 9: when something goes wrong

A failing step does not stop the engine. Submit a stack that does not
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

If the worker could not start at all, the message names the environment;
run that workflow's `environments/setup_env.py`.

## Where to go next

- The [README](README.md) of this part: every call and argument, and the
  full rules for scopes and workers.
- Part 2, [implement an analysis step](../2_implement_an_analysis_step/README.md).
- Part 3, [the workflows we use](../3_workflows_we_use/README.md).

---

MIT license. Thom de Hoog, Center for Microscopy and Image Analysis (ZMB),
University of Zurich. thom.dehoog@zmb.uzh.ch, thomdehoog@gmail.com.
