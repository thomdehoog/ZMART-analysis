# 1. Use the engine

This page shows how to run an analysis with the ZMART Analysis engine. For
a guided first run, open the [tutorial notebook](tutorial.ipynb).

## A complete run

```python
from zmart_analysis import Engine

engine = Engine()
engine.register("focus", "workflows/focus/pipelines/focus.yaml")
engine.submit("focus", {"image_path": "stack.tiff", "z_um": [0, 2, 4, 6, 8]})
engine.shutdown()                      # waits until the job has finished

for result in engine.results("focus"):
    print(result["score_focus"]["peak_z_um"])
```

You make an engine, register a recipe, submit images and collect the
results. The rest of this page explains each of these calls.

Three words come back throughout:

- A **recipe** is a YAML file. It lists the steps of an analysis, in order,
  with their parameters.
- A **step** is one Python file with one function, `run`.
- A **worker** is a Python process that runs a step, in the conda
  environment the step asks for.

```
your script ──► Engine ──► worker (conda env A) ──► step 1
                       ──► worker (conda env B) ──► step 2
            ◄── results ◄──
```

The engine never runs a step itself. It hands each job to a worker. A
worker starts the first time it is needed and then stays running, so a
model loaded for the first image is ready for the next one.

The conda environments must exist before the first run. Each workflow has a
script that makes them; see [Install it](../../README.md#install-it).

## Make an engine

```python
engine = Engine()
```

| Argument | Default | Meaning |
|---|---|---|
| `idle_timeout` | `300.0` | Seconds a worker may sit idle before it is stopped. `None` means never. |
| `max_concurrent` | `8` | How many jobs run at the same time. |
| `execution_timeout` | `300.0` | Seconds one step may run before it is stopped and its job fails. `None` means no limit. |

During an acquisition, the gap between two images is often longer than five
minutes. Use `Engine(idle_timeout=None)` there, so no worker is stopped
while the stage moves.

`with Engine() as engine:` shuts the engine down when the block ends.

## Register a recipe

```python
engine.register("focus", "workflows/focus/pipelines/focus.yaml")
```

`register(name, yaml_path)` reads the recipe and checks that every step
file exists. No step code runs yet. You choose the `name`, and every later
call uses it. Registering the same name twice raises `ValueError`.

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
engine.submit("focus", {"image_path": "stack.tiff", "z_um": [0, 2, 4, 6, 8]})
```

`submit(name, data, scope=None, priority=None, complete=None)` hands one
job to a recipe. It returns at once; the work happens in the background.

| Argument | Meaning |
|---|---|
| `data` | A dictionary with the input for this job. The recipe decides what belongs in it; the comment at the top of a shipped recipe says what to submit. |
| `priority` | A whole number. Higher runs first. Jobs with the same priority run in the order they were submitted. Default `0`. |
| `scope`, `complete` | Only for recipes with scoped steps. See [Steps that wait for a group of images](#steps-that-wait-for-a-group-of-images). |

Submitting to a name that is not registered raises `KeyError`. Submitting
after `shutdown` raises `RuntimeError`.

## Collect results

```python
for result in engine.results("focus"):
    print(result["score_focus"]["peak_z_um"])
```

`results(name)` returns the results that finished since the last call. Each
result is handed out once. A result is a dictionary:

| Key | What it is |
|---|---|
| the step's name | What that step produced. There is one key per step. |
| `input` | What you submitted. |
| `metadata` | The recipe's name and file, the steps that ran, the `scope` and the time. |
| `provenance` | Where each step ran: the `environment`, the `python` version, the versions of the `packages` it imported, and a `fingerprint` of everything installed. Two results with the same fingerprint ran in identical environments. |

To see how far a recipe is, ask for its status:

```python
engine.status("focus")
```
```
{'pending': 0, 'running': 1, 'completed': 7, 'failed': 1,
 'failures': [{'scope': {}, 'step': 'score_focus', 'error': '...',
               'phase': 0, 'submission_idx': 4}],
 'held': {'results': 0, 'units': []}}
```

A job is finished when it is counted under `completed` or `failed`.
`status()` without a name gives the status of every recipe.

## Steps that wait for a group of images

Some steps cannot run on a single image. A summary of a well needs every
image of that well. Such a step gets a `scope` in the recipe. It waits
until you say the group is complete, and then runs once on all its results.

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

A scoped step works on the results of the level below. Here
`summarise_population` receives one result per image, and
`compare_populations` receives one result per compartment.

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
  the same way. `status()` lists what is kept under `held`.

### The result of a scoped step

A scoped result holds what the step returned, with `metadata` and
`provenance` as above. It has no `input`. The engine adds:

| Key | What it is |
|---|---|
| `metadata["unit"]` | The unit this result is about, for example `{"carrier": 1, "compartment": 3}`. |
| `lineage` | What the result was built from. `submissions` lists every image of the unit, `failed` lists every failure in it, and `provenance` says where every earlier step ran. |

Every result also carries three keys that tell the two kinds apart:

| Key | Per-image result | Scoped result |
|---|---|---|
| `_scope_level` | `None` | The level, for example `"compartment"`. |
| `_phase` | `0` | `1` for the first scoped level in the recipe, `2` for the next, and so on. |
| `_scope` | The `scope` you submitted. | The `scope` of the submit that completed the unit. |

## When something fails

A failing step does not stop the engine. Only its own job fails, and the
other jobs carry on. You do not catch these errors. You read them in
`status`, under `failures`:

| Error | When |
|---|---|
| `StepExecutionError` | The step's `run` raised an error. The message holds that error's name and text. |
| `WorkerTimeoutError` | The step ran longer than `execution_timeout`. |
| `WorkerCrashedError` | The worker process died during the step. The engine starts a new worker for the next job. |
| `WorkerSpawnError` | The worker could not start. Usually the conda environment does not exist yet. Run the workflow's `environments/setup_env.py`. |

A failure leaves this list once a scoped step has been told about it. From
then on it is in that step's result, under `lineage`.

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

## Shut down

```python
engine.shutdown()
```

`shutdown()` stops accepting new work, lets the queued jobs finish and then
stops every worker. You can still collect results afterwards.

`shutdown(wait=False)` stops at once. It kills the workers, including a
step that is running, and the queued jobs do not run.

## What the engine does not do

- It does not decide what to image next. Your acquisition reads the results
  and decides.
- It does not store results. A step that needs a file writes it.
- It runs on one computer. It is not a cluster scheduler.
- It does not check step files. They run as they are, with your rights, so
  only run steps you trust.

Next: [implement an analysis step](../2_implement_an_analysis_step/README.md),
or [the workflows we use](../3_workflows_we_use/README.md).
