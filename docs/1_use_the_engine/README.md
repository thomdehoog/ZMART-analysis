# 1. Use the engine

This page shows how to run an analysis with the ZMART Analysis engine. For
a guided first run, open the [tutorial notebook](tutorial.ipynb).

> **Planned.** This page describes the next version of the engine: the job
> board, `deregister`, and workers that live with their recipe. The code does
> not do this yet. The tutorial notebook still shows the current version.

## A complete run

```python
from zmart_analysis import Engine

engine = Engine()
engine.register("my_pipeline", "path/to/my_pipeline.yaml")

job_1 = engine.submit("my_pipeline", {"image_path": "image1.tif"})
job_2 = engine.submit("my_pipeline", {"image_path": "image2.tif"})
job_3 = engine.submit("my_pipeline", {"image_path": "image3.tif"})

print(engine.status("my_pipeline"))
```
```
{1: {'step': 'segment', 'scope': {}, 'state': 'done',    'output': {...}},
 2: {'step': 'segment', 'scope': {}, 'state': 'running', 'output': None},
 3: {'step': 'segment', 'scope': {}, 'state': 'queued',  'output': None}}
```

You make an engine, register a recipe, submit images and read their output
from the job board. The rest of this page explains each of these calls.

Three words come back throughout:

- A **recipe** is a YAML file. It lists the steps of an analysis, in order,
  with their parameters.
- A **step** is one Python file with one function, `run`.
- A **worker** is a Python process that runs a step, in the conda
  environment the step asks for.

```
your script ──► Engine ──► worker (conda env A) ──► step 1
                       ──► worker (conda env B) ──► step 2
            ◄── job board ◄──
```

The engine never runs a step itself. It hands each job to a worker. A
worker starts the first time it is needed and then stays running for as long
as a recipe that needs it is registered, so a model loaded for the first
image is ready for the next one.

The conda environments must exist before the first run. Each workflow has a
script that makes them; see [Install it](../../README.md#install-it).

## Make an engine

```python
engine = Engine()
```

| Argument | Default | Meaning |
|---|---|---|
| `max_concurrent` | `8` | How many jobs run at the same time. |
| `execution_timeout` | `300.0` | Seconds one step may run before it is stopped and its job fails. `None` means no limit. |

Workers are not stopped while they wait. A worker stops only when you
deregister the last recipe that needs it, or shut the engine down.

`with Engine() as engine:` shuts the engine down when the block ends.

## Register a recipe

```python
engine.register("my_pipeline", "path/to/my_pipeline.yaml")
```

`register(name, yaml_path)` reads the recipe and checks that every step
file exists. No step code runs yet and no worker starts. You choose the
`name`, and every later call uses it. Registering the same name twice raises
`ValueError`; deregister it first.

A recipe looks like this:

```yaml
metadata:
  purpose: "Focus scoring for a z-stack"
  version: "1.0"
  functions_dir: "../steps"
  verbose: 1

focus:
  - score_focus:
      metric: brenner
      skip_ends: 2
```

The `metadata` block describes the recipe:

| Key | Meaning |
|---|---|
| `purpose`, `version` | Notes for the reader. |
| `functions_dir` | The folder with the step files, relative to the recipe. Default `../steps`. |
| `verbose` | `0` is silent. Above that, the steps print progress. Default `2`. |
| `levels` | Only for recipes with scoped steps. See [Steps that wait for a group of images](#steps-that-wait-for-a-group-of-images). |

The other top-level key, here `focus`, holds the steps in the order they
run. A step is named after its file: `score_focus` is `score_focus.py`.

Everything under a step is a parameter for that step, except two keys that
the engine reads itself:

| Key | Meaning |
|---|---|
| `scope` | Run this step once per group of images, not once per image. |
| `max_workers` | How many copies of this step may run at once. This overrides the value in the step file. |

A recipe cannot choose a step's conda environment. The step file does that
([part 2](../2_implement_an_analysis_step/README.md#metadata)).

## Submit images

```python
job = engine.submit("my_pipeline", {"image_path": "image.tif"})
```

`submit(name, data, scope=None, priority=None, complete=None)` hands one
job to a recipe. It returns the job's id at once; the work happens in the
background. Ids are whole numbers, counted per recipe from 1.

| Argument | Meaning |
|---|---|
| `data` | A dictionary with the input for this job. The recipe decides what belongs in it; the comment at the top of a shipped recipe says what to submit. |
| `priority` | A whole number. Higher runs first. Jobs with the same priority run in the order they were submitted. Default `0`. |
| `scope`, `complete` | Only for recipes with scoped steps. See [Steps that wait for a group of images](#steps-that-wait-for-a-group-of-images). |

Submitting to a name that is not registered raises `KeyError`. Submitting
after `shutdown` raises `RuntimeError`.

The first job of a step starts its worker. That job waits while the worker
starts; the jobs after it do not.

## The job board

```python
engine.status("my_pipeline")
```
```
{1: {'step': 'score_focus', 'scope': {'group': 2}, 'state': 'done', 'output': {...}},
 2: {'step': 'segment', 'scope': {'group': 2}, 'state': 'running', 'output': None},
 3: {'step': 'population_plots', 'scope': {'carrier': 1}, 'state': 'waiting', 'output': None}}
```

`status(name)` is the job board of a recipe: every job it has had, by id.
You poll it to see how far your jobs are and to read their output.

| Key | What it is |
|---|---|
| `step` | The step the job is at, or the last one it ran. |
| `scope` | The unit the job runs over. `{}` for a recipe without scopes. |
| `state` | `queued`, `running`, `waiting`, `done`, `failed` or `cancelled`. |
| `output` | What the steps returned, once the job is `done`. `None` before. |
| `error` | Only on a `failed` job: what went wrong. |

The states:

| State | Meaning |
|---|---|
| `queued` | Submitted, not started. |
| `running` | A step is running. |
| `waiting` | A scoped job waits for the images of its unit to finish. |
| `done` | Finished. The output is there. |
| `failed` | A step raised an error. |
| `cancelled` | Stopped by `deregister` or `shutdown` before it finished. |

`status()` without a name gives one board per recipe, keyed by the recipe's
name. Each recipe counts its own ids, so two recipes can both have a job 1.

The board keeps every job and its output until you deregister the recipe or
shut the engine down. Read what you need before that.

### The output

The output of a job is a dictionary:

| Key | What it is |
|---|---|
| the step's name | What that step produced. There is one key per step. |
| `input` | What you submitted. |
| `metadata` | The recipe's name and file, the steps that ran, the `scope` and the time. |
| `provenance` | Where each step ran: the `environment`, the `python` version, the versions of the `packages` it imported, and a `fingerprint` of everything installed. Two outputs with the same fingerprint ran in identical environments. |

## Steps that wait for a group of images

Some steps cannot run on a single image. A summary of a well needs every
image of that well. Such a step gets a `scope` in the recipe. It waits
until you say the group is complete, and then runs once on all their outputs.

Two more words:

- A **level** is a way to group images, such as `compartment` or `carrier`.
  You choose the names.
- A **unit** is one such group, such as compartment 3 of carrier 1.

The shipped recipes use three levels. A *carrier* is a plate or a slide. A
*compartment* is a well, a region of a slide, or part of a cleared sample.
A *group* is a set of images within a compartment.

### In the recipe

```yaml
metadata:
  functions_dir: "../steps"
  levels: [carrier, compartment, group]   # widest first

object_analysis:
  - detect_objects:               # every image, as soon as it arrives
  - extract_classical_features:
  - build_object_table:
  - summarise_population:         # once per compartment
      scope: compartment
  - compare_populations:          # once per carrier
      scope: carrier
```

Steps before the first `scope` run on every image. A step with a `scope`
runs once per unit of that level. The steps after it, up to the next
`scope`, run with it on the same unit.

A scoped step works on the outputs of the level below. Here
`summarise_population` receives one output per image, and
`compare_populations` receives one output per compartment.

### In your script

Every image says which units it belongs to. The last image of a unit says
that the unit is complete:

```python
engine.submit("analysis", image, scope={"carrier": 1, "compartment": 3})
engine.submit("analysis", last_image, scope={"carrier": 1, "compartment": 3},
              complete="compartment")
```

The engine never guesses when a unit is done. Your acquisition has to say
so.

One image can complete several levels. With
`complete=["compartment", "carrier"]` the compartment step runs first, then
the carrier step.

To run a step once over everything, use a level that no image names in its
`scope`, such as `all`. Put `scope: all` in the recipe and send
`complete="all"` with the last image.

### What the engine guarantees

- **Units with the same number stay apart.** Compartment 3 of carrier 1 is
  never mixed with compartment 3 of carrier 2. List your levels under
  `levels`, widest first, so this also holds for a level that has no step
  of its own.
- **A scoped step waits for its images.** After `complete`, the step waits
  until every image of the unit that was submitted so far has finished.
- **A wider unit waits for its narrower ones.** A carrier waits for a
  compartment that is still being summarised. For this to work, send a
  compartment's `complete` before its carrier's.
- **Nothing is dropped silently.** A carrier step is told which
  compartments failed, and which images belong to a compartment that was
  never completed. Those images are kept until their compartment is
  completed. An image that arrives after its unit was completed is kept in
  the same way.

### Scoped jobs on the board

Completing a unit adds a new job to the board: the scoped job. It has its
own id, the step it runs, and the unit as its `scope`. It is `waiting` until
the images of the unit have finished, then `running`, then `done`.

```
{7: {'step': 'summarise_population', 'scope': {'carrier': 1, 'compartment': 3},
     'state': 'waiting', 'output': None}}
```

`submit` returns the id of the image job only. You find the scoped job by
its `step` and `scope`. If the same unit is completed twice, the newer job
has the higher id.

### The output of a scoped job

A scoped output holds what the step returned, with `metadata` and
`provenance` as above. It has no `input`. The engine adds:

| Key | What it is |
|---|---|
| `metadata["unit"]` | The unit this result is about, for example `{"carrier": 1, "compartment": 3}`. |
| `lineage` | What the result was built from. `submissions` lists every image of the unit, `failed` lists every failure in it, and `provenance` says where every earlier step ran. |

## When something fails

A failing step does not stop the engine. Only its own job fails, and the
other jobs carry on. You do not catch these errors. You read them on the
board: the job's `state` is `failed`, and its `error` says what happened:

| Error | When |
|---|---|
| `StepExecutionError` | The step's `run` raised an error. The message holds that error's name and text. |
| `WorkerTimeoutError` | The step ran longer than `execution_timeout`. |
| `WorkerCrashedError` | The worker process died during the step. The engine starts a new worker for the next job. |
| `WorkerSpawnError` | The worker could not start. Usually the conda environment does not exist yet. Run the workflow's `environments/setup_env.py`. |

A failed image job stays on the board. A scoped step over its unit is also
told about it, under `lineage` in its output.

One error is raised by `submit` itself. `ScopeError` means that `complete`
names a level which the `scope` of the same call leaves out, although
earlier images did name it. An example is `scope={"carrier": 1},
complete="compartment"`. The engine refuses this, because it would complete
every compartment at once.

If a scoped step did not run and you want to know why, turn on the engine's
log:

```python
import logging
logging.basicConfig(level=logging.INFO)
```

## Deregister a recipe

```python
engine.deregister("my_pipeline")
```

`deregister(name)` is the end of a recipe:

1. The recipe stops accepting new images.
2. Its queued and running jobs finish. A scoped job whose unit was never
   completed does not run; it is `cancelled`.
3. Workers that no other registered recipe needs are stopped. A worker
   that another recipe still needs keeps running.
4. The recipe and its board are removed.

`deregister(name, wait=False)` stops at once. Running steps are killed and
queued jobs do not run; they are all `cancelled`.

Read the board before you deregister. Afterwards the name can be registered
again.

## Shut down

```python
engine.shutdown()
```

`shutdown()` deregisters every recipe and stops every worker. It lets the
queued jobs finish first. `shutdown(wait=False)` stops at once, like
`deregister(name, wait=False)` for every recipe.

## What the engine does not do

- It does not decide what to image next. Your acquisition reads the board
  and decides.
- It does not store outputs. A step that needs a file writes it.
- It runs on one computer. It is not a cluster scheduler.
- It does not check step files. They run as they are, with your rights, so
  only run steps you trust.

Next: [implement an analysis step](../2_implement_an_analysis_step/README.md),
or [the workflows we use](../3_workflows_we_use/README.md).
