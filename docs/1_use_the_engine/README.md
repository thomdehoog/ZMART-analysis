# 1. Use the engine

How to run an analysis with the ZMART Analysis engine. This page is the
reference. The [tutorial](tutorial.ipynb), a notebook, walks you through a first run.

## Contents

1. [The idea](#the-idea)
2. [Engine()](#engine)
3. [register](#register)
4. [Recipes](#recipes)
5. [submit](#submit)
6. [Scopes](#scopes)
7. [Workers](#workers)
8. [results](#results)
9. [status](#status)
10. [shutdown](#shutdown)
11. [Errors](#errors)
12. [What the engine does not do](#what-the-engine-does-not-do)
13. [All calls](#all-calls)

## The idea

An analysis is a *recipe*: a YAML file that lists steps in order, with their
parameters. Each step is one Python file with one function, `run`.

You register the recipe, submit images, and collect results.

```
your script ──► Engine ──► worker (conda env A) ──► step 1
                       ──► worker (conda env B) ──► step 2
            ◄── results ◄──
```

The engine never runs a step itself. It hands the work to a *worker*: a
Python process in the conda environment the step asks for. A worker starts
once and stays running, so a model loaded for the first image is still there
for the next. `submit` returns at once.

## Engine()

```python
from zmart_analysis import Engine

engine = Engine()
```

| Argument | Default | Meaning |
|---|---|---|
| `idle_timeout` | `300.0` | Seconds a worker may sit idle before it is stopped. `None`: never. |
| `max_concurrent` | `8` | How many jobs run at the same time. |
| `execution_timeout` | `300.0` | Seconds one step may run before it is killed and its job fails. `None`: no limit. |

During an acquisition the gap between images is often longer than five
minutes. Use `Engine(idle_timeout=None)` so no worker is lost while the
stage moves.

`with Engine() as engine:` shuts the engine down when the block ends.

## register

```python
engine.register("focus", "workflows/focus/pipelines/focus.yaml")
```

`register(name, yaml_path)` reads the recipe and the `METADATA` at the top
of each step file. No step code runs. The `name` is yours; every later call
uses it.

Registering a name twice raises `ValueError`. A missing step file raises
here, not later.

Each step runs in a conda environment that the workflow's
`environments/setup_env.py` makes. Run it once before the first `submit`
(see [Install it](../../README.md#install-it)).

## Recipes

```yaml
metadata:
  purpose: "Focus scoring for a z-stack"
  version: "1.0"
  functions_dir: "../steps"     # where the step files are, relative to this file
  levels: [carrier, compartment, group]   # the sample's levels, widest first (see Scopes)
  verbose: 1                    # 0 is silent; the steps print progress above that

focus:
  - score_focus:
      metric: brenner           # a parameter, handed to the step
      skip_ends: 2
```

- The first key that is not `metadata` names the recipe. The list under it
  is the steps, in the order they run.
- `purpose` and `version` are for the reader. `verbose` reaches every step
  in `pipeline_data["metadata"]`.
- A step is named after its file. `score_focus` is `score_focus.py` in
  `functions_dir` (default `../steps`).
- Everything under a step is a parameter for it, except two keys the engine
  keeps:

| Key | Meaning |
|---|---|
| `scope` | Run this step once per unit (a compartment, a carrier, ...), not once per image. See [Scopes](#scopes). |
| `max_workers` | How many copies of this step may run at once. Overrides the step's `METADATA`. |

A step with a `scope` starts a new phase. The steps after it, until the
next `scope`, run in that same phase, on the same unit.

A recipe cannot set a step's `environment`; the step file does
([part 2](../2_implement_an_analysis_step/README.md#metadata)).

## submit

```python
engine.submit("focus", {"image_path": "stack.tiff", "z_um": [0, 2, 4, 6, 8]})
```

`submit(name, data, scope=None, priority=None, complete=None)` hands one job
to a recipe and returns at once.

| Argument | Meaning |
|---|---|
| `data` | A dictionary. The steps see it as `pipeline_data["input"]`. What it holds is up to the recipe. |
| `scope` | The units this image belongs to, e.g. `{"carrier": 1, "compartment": 3}`. Only for recipes with scoped steps. |
| `priority` | Whole number. Higher runs first; equal runs in submission order. Default `0`. |
| `complete` | The level, or levels, this image closes: `"compartment"` or `["compartment", "carrier"]`. |

Submitting to an unknown name raises `KeyError`. After `shutdown`,
`RuntimeError`.

## Scopes

Some analysis runs per image. Some can only start when a whole unit is in:
a stitch needs every image of a region, a summary needs every region. A
step with a `scope` waits for that.

The levels are names of your choosing. The shipped scoped recipe uses,
narrowest first, **group**, **compartment** and **carrier**. A compartment
is a well, a region of a slide, or part of a cleared sample. A carrier is
the plate or the slide. The recipe lists its levels, widest first, in
`metadata.levels`.

Each image is submitted with the units it belongs to. The image that closes
a unit says so:

```python
engine.submit("analysis", image, scope={"carrier": 1, "compartment": 3})
engine.submit("analysis", last_image, scope={"carrier": 1, "compartment": 3},
              complete="compartment")
```

In the recipe, steps before the first `scope` run on every image as it
comes in. A step with `scope: compartment` runs once per compartment, after
`complete="compartment"`, on that compartment's results. A later step with
`scope: carrier` runs once per carrier, on its compartments:

```yaml
object_analysis:
  - detect_objects:               # every image, as soon as it lands
  - extract_classical_features:
  - summarise_population:         # each compartment, once it is complete
      scope: compartment
  - compare_populations:          # each carrier, once it is complete
      scope: carrier
```

What a scoped step receives is in
[part 2](../2_implement_an_analysis_step/README.md#a-step-over-a-scope).

### The rules

- **A unit keeps its wider levels.** Compartment 3 of carrier 1 never mixes
  with compartment 3 of carrier 2. With `levels` declared, this holds for
  levels the recipe has no step for as well.
- **A unit waits for its images.** On `complete`, the step waits until
  every image of the unit submitted so far has finished.
- **A wider unit waits for its narrower ones.** A carrier waits for a
  compartment still being summarised, if that compartment was closed before
  the carrier. Send a compartment's `complete` before its carrier's.
- **Nothing is lost quietly.** A carrier step is told about a compartment
  that failed, and about images of a compartment that was never closed.
  Those images are kept, listed in `status()` under `held`, until their
  compartment is closed. An image that arrives after its unit was closed
  is held the same way.

One image may close several levels: `complete=["compartment", "carrier"]`
runs the compartment step, then the carrier step.

A level no image names in its `scope`, such as `all`, collects everything:
`scope: all` in the recipe, `complete="all"` on the last image.

## Workers

Every step runs in a worker: a Python process in the conda environment
named in the step's `METADATA`. A step that names none, or names the
engine's own, runs in a worker started with the engine's own Python.

- A worker starts the first time its step is needed and stays running. A
  step keeps anything expensive, such as a loaded model, in `state`.
- `max_workers` above one gives a step that many workers.
- `idle_timeout` stops a worker that sits idle; `execution_timeout` kills
  a step that runs too long. See [Engine()](#engine).
- A crashing step fails only its own job. The worker is replaced.

## results

```python
for result in engine.results("focus"):
    print(result["score_focus"]["peak_z_um"])
```

`results(name)` returns every result finished since the last call and
removes them from the queue. Each is a dictionary:

| Key | What it is |
|---|---|
| the step's name | What that step published. One key per step. |
| `input` | What was submitted. |
| `metadata` | Recipe name and file, the steps that ran, the `scope`, when it ran. |
| `provenance` | Per step: `environment`, `python`, a `fingerprint` of the installed packages, the versions of the `packages` it imported. What ran, not what was asked. |
| `lineage` | Scoped results only. `submissions`: every image under this unit. `failed`: every failure under it. `provenance`: every step below, one record per distinct environment. |
| `_phase` | `0` for a per-image result. `1`, `2`, ... for a scoped one. |
| `_scope` | The `scope` of the submit. For a scoped result, of the submit that closed it. The unit itself is `metadata["unit"]`. |
| `_scope_level` | The level of a scoped result. `None` per image. |

## status

```python
engine.status("focus")
```
```
{'pending': 0, 'running': 1, 'completed': 7, 'failed': 1,
 'failures': [{'scope': {}, 'step': 'score_focus', 'error': '...',
               'phase': 0, 'submission_idx': 4}],
 'held': {'results': 0, 'units': []}}
```

`status(name)` counts jobs waiting, running, done and failed, and lists each
failure. A failure leaves this list once a scoped step has been told about
it; it is then in that step's result, under `lineage`. `held` is what waits
for a unit to close. `status()` gives this for every recipe.

## shutdown

```python
engine.shutdown()
```

`shutdown(wait=True)` stops accepting work, lets queued jobs finish, and
stops every worker. `shutdown(wait=False)` kills the workers first, a step
in flight included.

## Errors

A failing step does not stop the engine. Its job counts under `failed` in
`status`, with the error's name and message under `failures`. The other
jobs carry on. You do not catch these; you read them in `status`:

| In `failures` | When |
|---|---|
| `StepExecutionError` | The step's `run` raised. The message is the exception's name and text. |
| `WorkerTimeoutError` | The step ran longer than `execution_timeout`. |
| `WorkerCrashedError` | The worker process died during the step. |
| `WorkerSpawnError` | The worker could not start. Usually the conda environment does not exist: run the workflow's `environments/setup_env.py`. |

Two kinds of error are raised by the calls themselves, so you see them at
once:

| Raised | When |
|---|---|
| `ValueError`, `KeyError`, `RuntimeError` | A bad recipe or name at `register` or `submit`; a call after `shutdown`. |
| `ScopeError` | A `complete` for a level the recipe has a step for, which earlier images named in their `scope` and this one leaves out: `scope={"carrier": 1}, complete="compartment"` after images with a `compartment`. That would close every compartment at once. |

The engine logs what it does through Python's `logging`. To see why a
scoped step did not run:

```python
import logging
logging.basicConfig(level=logging.INFO)
```

## What the engine does not do

- It does not decide what to image next. The acquisition reads the results
  and decides.
- It does not store results. Steps that need files write them.
- It runs on one computer. It is not a cluster scheduler.
- It runs step files as they are, with your rights. Only run steps you
  trust.

## All calls

| Call | What it does | Returns |
|---|---|---|
| `Engine(idle_timeout=300.0, max_concurrent=8, execution_timeout=300.0)` | Make an engine | the engine |
| `register(name, yaml_path)` | Read a recipe and its steps' `METADATA` | nothing |
| `submit(name, data, scope=None, priority=None, complete=None)` | Hand one job to a recipe | nothing |
| `results(name)` | The results finished since the last call | list of dicts |
| `status(name=None)` | Jobs waiting, running, done, failed, held | dict |
| `shutdown(wait=True)` | Stop the engine and its workers | nothing |

Next: [implement an analysis step](../2_implement_an_analysis_step/README.md),
or [the workflows we use](../3_workflows_we_use/README.md).
