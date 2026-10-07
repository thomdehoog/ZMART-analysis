# 1. Use the engine

Every call of the ZMART Analysis engine, from the side of the person who
runs an analysis.

This page is the documentation of the engine. For a step-by-step walk-through,
see the [tutorial](tutorial.md).

## Contents

1. [The idea](#the-idea)
2. [Engine()](#engine)
3. [register](#register)
4. [Recipes](#recipes)
5. [submit](#submit)
6. [Scopes](#scopes)
7. [Workers](#workers)
8. [results and status](#results-and-status)
9. [shutdown](#shutdown)
10. [Errors](#errors)
11. [What the engine does not do](#what-the-engine-does-not-do)
12. [All calls at a glance](#all-calls-at-a-glance)

## The idea

An analysis is a *recipe*: a YAML file that lists the steps, in order, with
their parameters. Each step is one Python file with one function, `run`.
You register the recipe, submit images, and collect the results.

```
your script ──► Engine ──► worker (conda env A) ──► step 1
                       ──► worker (conda env B) ──► step 2
            ◄── results ◄──
```

The engine never runs a step itself. It reads each step file only for the
`METADATA` at its top, without executing the file, and hands the work to a
*worker*: a Python process inside the conda environment the step asked for.
A worker starts once and stays running, so a model loaded for the first
image is still there for the next. `submit` returns at once.

## Engine()

```python
from engine import Engine

engine = Engine()
```

| Argument | Default | Meaning |
|---|---|---|
| `idle_timeout` | `300.0` | Seconds a worker may sit idle before it is stopped. `None` keeps every worker as long as the engine lives. |
| `max_concurrent` | `8` | How many jobs the engine handles at the same time. |
| `execution_timeout` | `300.0` | Seconds one step may run before its worker is killed and the job fails. `None` for no limit. |

During an acquisition the gaps between images are often longer than five
minutes. Use `Engine(idle_timeout=None)` so no worker is lost while the
stage moves.

`with Engine() as engine:` shuts the engine down, workers included, when
the block ends.

## register

```python
engine.register("focus", "workflows/focus/pipelines/focus.yaml")
```

`register(name, yaml_path)` reads the recipe and the `METADATA` of each
step it names, without running any step code. The `name` is yours; every
later call uses it. Registering a name twice raises `ValueError`; a
missing step file raises here, not later.

## Recipes

```yaml
metadata:
  functions_dir: "../steps"     # where the step files are, relative to this file
  verbose: 1                    # handed to every step in pipeline_data["metadata"]

focus:
  - score_focus:
      metric: brenner           # a parameter, handed to the step
      skip_ends: 2
```

- The first key that is not `metadata` names the recipe; the list under it
  is the steps, in the order they run.
- A step is named after its file: `score_focus` is `steps/score_focus.py`
  in `functions_dir` (default `../steps`).
- Everything under a step is handed to it as a parameter, except two keys
  the engine keeps:

| Key | Meaning |
|---|---|
| `scope` | Run this step once per unit (a group, a compartment, ...), not once per image. See [Scopes](#scopes). |
| `max_workers` | How many copies of this step may run at once. Overrides the step's `METADATA`. |

A recipe never sets a step's `environment`; that belongs in the step file,
beside the imports that need it. A recipe that tries is refused.

## submit

```python
engine.submit("focus", {"image_path": "stack.tiff", "z_um": [0, 2, 4, 6, 8]})
```

`submit(name, data, scope=None, priority=None, complete=None)` hands one
job to the recipe and returns immediately.

| Argument | Meaning |
|---|---|
| `data` | A dictionary; the steps find it as `pipeline_data["input"]`. What it must contain is up to the recipe. |
| `scope` | The units this image belongs to, e.g. `{"carrier": 1, "compartment": 3}`. Only for recipes with scoped steps. |
| `priority` | Whole number, higher runs first; equal priorities run in submission order. Default `0`. |
| `complete` | A level, or list of levels, this image closes: `"compartment"` or `["compartment", "carrier"]`. |

Submitting to an unregistered name raises `KeyError`; after `shutdown`,
`RuntimeError`.

## Scopes

Some analysis is per image. Some can only start once a whole unit is in: a
stitch needs every image of a region, a summary needs every region. A step
with a `scope` waits for that.

The levels are plain names the engine attaches no meaning to. The shipped
workflows use, narrowest first, **image**, **group**, **compartment**,
**carrier** and **all**. A compartment can be a well, a region of a slide
or part of a cleared sample; a carrier is the plate or the slide. A recipe
names its levels, widest first, in its metadata:

```yaml
metadata:
  levels: [carrier, compartment, group]
```

Each image is submitted with the units it belongs to; the image that closes
a unit says so:

```python
engine.submit("analysis", image, scope={"carrier": 1, "compartment": 3})
engine.submit("analysis", last_image, scope={"carrier": 1, "compartment": 3},
              complete="compartment")
```

In the recipe, steps before the first `scope` run on every image as it
comes in. A step with `scope: compartment` runs once per compartment, after
`complete="compartment"`, on the results of that compartment's images. A
later step with `scope: carrier` runs once per carrier on its compartments:

```yaml
object_analysis:
  - detect_objects:               # every image, as soon as it lands
  - extract_classical_features:
  - summarise_population:         # each compartment, once it is complete
      scope: compartment
  - compare_populations:          # each carrier, once it is complete
      scope: carrier
```

A scoped step receives `results` (the collected results, in submission
order), `failures` (what failed under this unit) and `metadata` with its
`scope_level`, the closing `scope` and the `unit` it is for, widest level
first. See [what a scoped step receives](../2_implement_an_analysis_step/README.md#a-step-over-a-scope).

Four rules keep this correct while the acquisition is still going:

- **A unit is matched with every wider level.** Compartment 3 of carrier 1
  never mixes with compartment 3 of carrier 2. With `levels` declared this
  holds for levels the recipe has no step for too: a group stays inside
  its compartment in a recipe that summarises groups and compares
  carriers. Without `levels`, only the scopes of later steps count.
- **A scoped step waits for its images.** On `complete`, it waits until
  every image of the unit submitted so far has finished its own steps.
- **A wider unit waits for its narrower ones.** A carrier step waits for
  any compartment still being summarised, if that compartment was closed
  before the carrier. Send a compartment's `complete` before its
  carrier's: a carrier does not wait for a signal sent after it.
- **Nothing is lost quietly.** A carrier step is told about a compartment
  step that failed, and about the images of a compartment not closed when
  the carrier was (as failures with `step: "engine"`). Those images are
  kept, and `status()` lists them under `held`, until their compartment
  is closed. An image that arrives after its unit was closed waits the
  same way, until the unit is closed again.

One image may close several levels: `complete=["compartment", "carrier"]`
runs the compartment step, then the carrier step. A level no image names
in its `scope`, such as `all`, collects everything from the previous phase:
`scope: all` in the recipe, `complete="all"` on the last image.

A `complete` whose scope leaves out the level it closes, such as
`scope={"carrier": 1}, complete="compartment"`, would close every
compartment at once. Once any image has named that level, `submit` raises
`ScopeError` instead; the very first image of a recipe is not checked.

The engine never decides on its own that a unit is complete. The
acquisition knows, and says so. Until a unit is closed its results stay in
memory.

## Workers

Every step runs in a worker: a Python process inside the conda environment
named in the step's `METADATA`. A step that names none, or names the
engine's own environment, runs in the engine's Python.

- A worker starts the first time its step is needed and stays running. A
  step keeps anything expensive, such as a loaded model, in `state`, a
  dictionary that survives from one job to the next in that worker.
- `max_workers` above one gives a step that many workers, so that many
  images are analysed at once.
- A worker idle longer than `idle_timeout` is stopped; the next job starts
  a fresh one. A step running longer than `execution_timeout` is killed and
  its job fails.
- A crashing step fails only its own job. The worker is replaced and the
  other jobs carry on.

## results and status

```python
for result in engine.results("focus"):
    print(result["score_focus"]["peak_z_um"])
```

`results(name)` returns every result finished since you last asked and
removes them from the queue. Each is a dictionary with:

| Key | What it is |
|---|---|
| the step's name | What that step published, one key per step. |
| `input` | What was submitted. |
| `metadata` | Recipe name and file, the steps that ran, the `scope`, when it ran. |
| `provenance` | Per step: the conda `environment` it ran in, its `python` version, a `fingerprint` of the installed packages, and the versions of the `packages` the step imported. The recipe says what was asked for; this says what actually ran. |
| `lineage` | Scoped results only. `submissions`: the index of every image under this unit. `failed`: every failure under it. `provenance`: for every step below this one, its distinct records; two records for one step mean the environment changed during the run. |
| `_phase` | `0` for a per-image result; `1`, `2`, ... for a scoped one. |
| `_scope`, `_scope_level` | The scope of the submit that closed a scoped result, and at which level. `None` per image. The unit itself is `metadata["unit"]`. |

```python
engine.status("focus")
```
```
{'pending': 0, 'running': 1, 'completed': 7, 'failed': 1,
 'failures': [{'scope': {}, 'step': 'score_focus', 'error': '...',
               'phase': 0, 'submission_idx': 4}],
 'held': {'results': 0, 'units': []}}
```

`status(name)` counts jobs waiting, running, done and failed, and lists
each failure with its step, message, phase and submission index. A failure
leaves this list once a scoped step has been told about it; it is then in
that step's `failures` and in its result's `lineage`. `held` is what waits
for a unit to close: how many results, and which units. `status()` gives
this for every recipe.

## shutdown

```python
engine.shutdown()
```

`shutdown(wait=True)` stops accepting work, lets queued jobs finish, and
stops every worker. `shutdown(wait=False)` kills the workers first, a step
in flight included.

## Errors

A failing step does not stop the engine. Its job is counted under `failed`
in `status`, and the other jobs carry on.

| Error | When |
|---|---|
| `StepExecutionError` | The step's `run` raised. The worker's traceback is on `.remote_traceback`. |
| `WorkerTimeoutError` | The step ran longer than `execution_timeout`. |
| `WorkerCrashedError` | The worker process died during the step. |
| `WorkerSpawnError` | The worker could not start, usually because the conda environment does not exist. Run the workflow's `environments/setup_env.py`. |
| `ScopeError` | Raised by `submit`: a `complete` whose scope leaves out the level it closes, after earlier images named that level. See [Scopes](#scopes). |

The four worker errors share the base `WorkerError`. All are importable
from `engine`.

## What the engine does not do

- It does not decide what to image next. The acquisition reads the results
  and decides.
- It does not store results. Steps that need files write them themselves.
- It runs on one computer; it is not a cluster scheduler.
- It runs step files as they are, with your rights. Only run steps you
  trust.

## All calls at a glance

| Call | What it does | Gives back |
|---|---|---|
| `Engine(idle_timeout=300, max_concurrent=8, execution_timeout=300)` | Make an engine | the engine |
| `register(name, yaml_path)` | Read a recipe and its steps' `METADATA` | nothing |
| `submit(name, data, scope=None, priority=None, complete=None)` | Hand one job to a recipe; returns at once | nothing |
| `results(name)` | The results finished since the last call | list of dicts |
| `status(name=None)` | Jobs waiting, running, done and failed | dict |
| `shutdown(wait=True)` | Stop accepting work, finish or kill, stop the workers | nothing |

Next: [implement an analysis step](../2_implement_an_analysis_step/README.md),
or [the workflows we use](../3_workflows_we_use/README.md).
