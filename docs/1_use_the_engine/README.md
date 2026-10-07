# 1. Use the engine

Every call of the ZMART Analysis engine, from the side of the person who
runs an analysis.

This page is the documentation of the engine. For a step-by-step walk-through, see the [tutorial](tutorial.md).

For writing a step of your own, see [part 2](../2_implement_an_analysis_step/README.md),
and for the workflows that ship with the engine, [part 3](../3_workflows_we_use/README.md).
Everything below was checked against the code in `engine/` and its tests in
`tests/test_engine.py`.

## Contents

1. [The idea](#the-idea)
2. [Make an engine: Engine()](#make-an-engine-engine)
3. [Register a recipe: register](#register-a-recipe-register)
4. [Recipes](#recipes)
5. [Submit an image: submit](#submit-an-image-submit)
6. [Scopes](#scopes)
7. [Workers](#workers)
8. [What comes back: results, status](#what-comes-back-results-status)
9. [Stop: shutdown](#stop-shutdown)
10. [When something goes wrong](#when-something-goes-wrong)
11. [What the engine does not do](#what-the-engine-does-not-do)
12. [All calls at a glance](#all-calls-at-a-glance)

## The idea

An analysis is a *recipe*: a YAML file that lists the steps, in order, with
their parameters. Each step is one Python file with one function, `run`. You
register the recipe with the engine, submit images to it, and collect the
results.

```
your script ──► Engine ──► worker (conda env A) ──► step 1
                       ──► worker (conda env B) ──► step 2
            ◄── results ◄──
```

The engine itself never runs a step. It reads each step file only to find
its `METADATA` at the top, without executing the file, and hands the work
to a *worker*: a Python process running inside the conda environment that
step asked for. A worker starts once and stays running, so a model loaded
for the first image is still there for the next.

`submit` returns at once. The microscope is never kept waiting; you collect
the results when you want them.

## Make an engine: Engine()

```python
from engine import Engine

engine = Engine()
```

| Argument | Default | Meaning |
|---|---|---|
| `idle_timeout` | `300.0` | Seconds a worker may sit idle before it is stopped to free memory. `None` keeps every worker for as long as the engine lives, so the cost of importing a step's packages is paid once per session. |
| `max_concurrent` | `8` | How many jobs the engine itself handles at the same time. |
| `execution_timeout` | `300.0` | Seconds one step may run before its worker is killed and the job is reported as failed. `None` for no limit. |

During an acquisition, gaps between images are often longer than five
minutes. Make the engine with `idle_timeout=None` for a session, so no
worker is lost while the stage moves.

The engine is a context manager. `with Engine() as engine:` shuts it down,
and all its workers, when the block ends.

## Register a recipe: register

```python
engine.register("focus", "workflows/focus/pipelines/focus.yaml")
```

`register(name, yaml_path)` reads the recipe and, for each step it names,
opens the step file and reads its `METADATA` without running any code. From
that it knows which environment each step needs and how many copies of it
may run at once. The `name` is yours to choose; every later call uses it.

Registering the same name twice raises `ValueError`. A step file that is
missing raises at this point, not later.

## Recipes

A recipe is a YAML file with an optional `metadata` block and one list of
steps under the recipe's name:

```yaml
metadata:
  functions_dir: "../steps"     # where the step files are, relative to this file
  verbose: 1                    # handed to every step in pipeline_data["metadata"]

focus:
  - score_focus:
      metric: brenner           # a parameter: handed to the step
      skip_ends: 2              # a parameter: handed to the step
```

- The first key that is not `metadata` is the recipe's name, and the list
  under it is the steps, in the order they run.
- Each step is named after its file: `score_focus` is
  `steps/score_focus.py` in `functions_dir`. If `functions_dir` is not
  given, it is `../steps`.
- Everything written under a step is handed to the step as a parameter,
  except two keys the engine keeps for itself:

| Key | Meaning |
|---|---|
| `scope` | Run this step once per *unit* (a group, a compartment, ...), not once per image. See [Scopes](#scopes). |
| `max_workers` | How many copies of this step may run at the same time. Overrides the step file's own `METADATA`. |

A recipe never says which environment a step runs in. That belongs to the
step file, beside the imports that need it. A recipe that sets
`environment` on a step is refused with a message saying so.

The recipe is the record of what was asked for. Share the file, and a
colleague runs exactly the same analysis.

## Submit an image: submit

```python
engine.submit("focus", {"image_path": "stack.tiff", "z_um": [0, 2, 4, 6, 8]})
```

`submit(name, data, scope=None, priority=None, complete=None)` hands one
job to the recipe and returns immediately.

| Argument | Meaning |
|---|---|
| `data` | A dictionary. The steps find it as `pipeline_data["input"]`. What goes in it is up to the recipe; each recipe's own notes say what it expects. |
| `scope` | Which units this image belongs to, for example `{"carrier": 1, "compartment": 3}`. Only needed for recipes with scoped steps. |
| `priority` | A whole number; higher runs first. Jobs with the same priority run in the order they were submitted. Default `0`. |
| `complete` | A level, or a list of levels, that this image closes: `"compartment"`, or `["compartment", "carrier"]`. See [Scopes](#scopes). |

Submitting to a name that was never registered raises `KeyError`.
Submitting after `shutdown` raises `RuntimeError`.

## Scopes

Some analysis is per image. Some can only start once a whole unit is in:
a stitch needs every image of a region, a summary needs every region of the
sample. A step with a `scope` waits for that.

The levels are plain names; the engine attaches no meaning to them. The
workflows that ship use, narrowest first, **image**, **group**,
**compartment**, **carrier** and **experiment**. A compartment can be a
well of a plate, a region of a slide or part of a cleared sample; a carrier
is the plate or the slide.

Each image is submitted with the units it belongs to, and the image that
closes a unit says so:

```python
engine.submit("analysis", image, scope={"carrier": 1, "compartment": 3})
engine.submit("analysis", last_image, scope={"carrier": 1, "compartment": 3},
              complete="compartment")
```

In the recipe, the steps before the first `scope` run on every image as it
comes in. A step with `scope: compartment` runs once per compartment, after
`complete="compartment"` arrives, on the results of every image of that
compartment. A step after it with `scope: carrier` runs once per carrier on
the results of its compartments:

```yaml
object_analysis:
  - detect_objects:               # every image, as soon as it lands
  - extract_classical_features:
  - summarise_population:         # each compartment, once it is complete
      scope: compartment
  - compare_populations:          # each carrier, once it is complete
      scope: carrier
```

A scoped step receives a different `pipeline_data`: `results`, the list of
results it collects, in submission order; `failures`, the jobs of that unit
that failed; and `metadata`, with the `scope_level` and `scope` it runs for.

Three rules keep this correct while the acquisition is still going on:

- **A unit is matched with every wider level.** Compartment 3 of carrier 1
  never mixes with compartment 3 of carrier 2, so two carriers can be
  acquired at once.
- **A scoped step waits for its images.** When `complete` arrives, the step
  waits until every image of that unit has finished its own steps.
- **A wider unit waits for its narrower ones.** A carrier step waits for any
  of its compartments still being summarised, even when the two signals
  arrive in quick succession.

One image may close several levels at once: `complete=["compartment",
"carrier"]` runs the compartment step first, then the carrier step.

A level that no image names in its `scope`, such as `all` or `experiment`,
collects everything from the previous phase. That is how a final step over
the whole run is written: `scope: experiment` in the recipe,
`complete="experiment"` on the last image.

The engine never decides on its own that a unit is complete. The
acquisition knows, and says so. Until a unit is closed, its results are
kept in memory, so a recipe with scopes is for runs that close their units.

## Workers

Every step runs in a worker: a Python process started inside the step's
conda environment, named in its `METADATA`. A step that names no
environment, or names the one the engine runs in, runs in the engine's own
Python.

- A worker is started the first time a step needs it, then stays running
  and takes one job after another. A step keeps anything expensive, such
  as a model on the GPU, in a small dictionary called `state`, which
  survives from one job to the next inside that worker.
- One environment can have several workers when a step's `max_workers` is
  more than one. Then that many images are analysed at the same time.
- A worker idle for longer than `idle_timeout` is stopped; the next job
  starts a fresh one. `idle_timeout=None` keeps them all.
- A step that runs longer than `execution_timeout` is stopped, and its job
  is reported as failed.
- A step that crashes takes down only its own job. The worker is replaced
  and the other jobs carry on.

The engine talks to its workers over a private connection on the same
computer, protected by a random key, and passes the data as Python objects.

## What comes back: results, status

```python
for result in engine.results("focus"):
    print(result["score_focus"]["peak_z_um"])
```

`results(name)` returns every result finished since you last asked, and
removes them from the queue. Each is a dictionary with:

| Key | What it is |
|---|---|
| the step's name | What that step published, one key per step. |
| `input` | What was submitted. |
| `metadata` | The recipe's name and file, the steps that ran, the `scope`, when it ran. |
| `provenance` | One entry per step: the conda `environment` it ran in, its `python` version, a `fingerprint` of every installed package, and the versions of the `packages` the step imported. The recipe says what was asked for; this says what actually ran. |
| `_phase` | `0` for a per-image result; `1`, `2`, ... for a scoped one. |
| `_scope`, `_scope_level` | Which unit a scoped result is for, and at which level. `None` for a per-image result. |

```python
engine.status("focus")
```
```
{'pending': 0, 'running': 1, 'completed': 7, 'failed': 1,
 'failures': [{'scope': {}, 'step': 'score_focus', 'error': '...'}]}
```

`status(name)` counts the jobs that are waiting, running, done and failed,
and lists each failure with the step and its message. `status()` with no
name gives this for every registered recipe.

## Stop: shutdown

```python
engine.shutdown()
```

`shutdown(wait=True)` stops accepting work, lets the queued jobs finish, and
stops every worker. `shutdown(wait=False)` is the interrupt: it kills the
workers first, a step in flight included, so nothing queued runs. The
`with Engine() as engine:` form calls `shutdown()` for you.

## When something goes wrong

A step that fails does not stop the engine. Its job is counted under
`failed` in `status`, with the step name and the error message, and the
other jobs carry on. The errors are:

| Error | When |
|---|---|
| `StepExecutionError` | The step's `run` raised. The traceback from inside the worker is on `.remote_traceback`. |
| `WorkerTimeoutError` | The step ran longer than `execution_timeout`. |
| `WorkerCrashedError` | The worker process died while running the step. |
| `WorkerSpawnError` | The worker could not start, usually because the conda environment does not exist. Run the workflow's `environments/setup_env.py`. |
| `ScopeError` | A scope was configured or signalled in a way the engine cannot act on. |

All four worker errors share the base `WorkerError`. They are importable
from `engine`.

## What the engine does not do

- It does not decide what to image next. The acquisition software reads
  the results and decides.
- It does not store results. Steps that need files write them themselves,
  usually beside the images.
- It runs on one computer. It is not a cluster scheduler.
- It runs step files as they are, with your rights. Only run steps you
  trust.

## All calls at a glance

| Call | What it does | What it gives back |
|---|---|---|
| `Engine(idle_timeout=300, max_concurrent=8, execution_timeout=300)` | Make an engine | the engine |
| `register(name, yaml_path)` | Read a recipe and the `METADATA` of its steps | nothing |
| `submit(name, data, scope=None, priority=None, complete=None)` | Hand one job to a recipe; returns at once | nothing |
| `results(name)` | The results finished since the last call | list of dicts |
| `status(name=None)` | Jobs waiting, running, done and failed | dict |
| `shutdown(wait=True)` | Stop accepting work, finish or kill, stop the workers | nothing |
