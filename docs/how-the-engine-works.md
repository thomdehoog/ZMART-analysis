# How the engine works

This page follows one tile through the engine, then explains the parts it
passed through. For writing your own step, see
[Writing a step](writing-a-step.md).

## One tile, start to finish

```python
from engine import Engine

with Engine() as engine:
    engine.register("analysis", "workflows/object_analysis/pipelines/object_analysis.yaml")
    engine.submit("analysis", {"image_path": "tile_0001.ome.tiff", ...})
    ...
    results = engine.results("analysis")
```

1. **`register`** reads the recipe. For each step it finds the step file and
   reads the `METADATA` at its top, without running any of the file's code.
   From that it knows which environment each step needs.
2. **`submit`** hands the tile to the engine and returns at once. The
   acquisition is never kept waiting.
3. The engine gives the tile to the first step's **worker**, a program
   already running in that step's environment. The step's result goes on to
   the next step, which may run in a different worker and environment.
4. When the last step is done, the result waits in a queue until you
   collect it with **`results`**.

## Recipes

A recipe is a YAML file with a short `metadata` block and one list of steps:

```yaml
metadata:
  functions_dir: "../steps"    # where the step files are, from this file

object_analysis:
  - detect_objects:
      diameter: 30             # passed to the step as a parameter
      max_workers: 1           # for the engine: how many may run at once
  - extract_classical_features:
  - summarise_population:
      scope: compartment       # for the engine: when to run
```

Each step is named after its file: `detect_objects` is
`steps/detect_objects.py`. Every entry under a step is handed to the step as
a parameter, except two that the engine keeps for itself:

| Key | Meaning |
|---|---|
| `scope` | Run this step once a unit is complete, not once per tile. See *Scopes* below. |
| `max_workers` | How many copies of this step may run at the same time. |

A recipe never says which environment a step runs in. That belongs to the
step file, beside the imports that need it, and a recipe that tries to set
it is refused with a message saying so.

## Workers

Each environment gets its own worker program, started the first time a step
needs it. It then stays running and takes one job after another. A step
keeps anything expensive, such as a model loaded onto the GPU, in a small
dictionary called `state`, which survives from one job to the next inside
that worker.

- A worker that has been idle for five minutes is stopped, to free its
  memory. The next job starts a fresh one.
- A step that runs longer than five minutes is stopped, and its job is
  reported as failed. Both limits can be changed when the `Engine` is made.
- A step that crashes takes down only its own job. The worker is replaced
  and the other jobs carry on.

The engine talks to its workers over a private connection on the same
computer, protected by a random password, and sends the data to each worker
as Python objects.

## Scopes

A sample is divided into levels, narrowest first: tile, group, compartment,
carrier. Each tile is submitted with the units it belongs to:

```python
engine.submit("analysis", tile, scope={"carrier": 1, "compartment": 3, "group": 2})
```

A step with `scope: compartment` does not run per tile. It waits until a
submit says `complete="compartment"`, then runs once with the results of
every tile of that compartment, in the order they were submitted. A step
after it with `scope: carrier` runs once the carrier is complete, with the
results of every compartment of that carrier.

Three rules keep this correct while acquisition is still going on:

- **A unit is matched with every wider level.** Compartment 3 of carrier 1
  never mixes with compartment 3 of carrier 2, so two carriers can be
  acquired at once.
- **A scoped step waits for its tiles.** When `complete` arrives, the step
  waits until every tile of that unit has finished its own steps.
- **A wider unit waits for its narrower ones.** A carrier step waits for any
  of its compartments still being summarised, even when the two signals
  arrive in quick succession.

The engine never decides on its own that a unit is complete. Tiles are kept
in memory until their unit is closed, so a recipe with scopes is for runs
that close their units.

## What comes back

`engine.results(name)` returns every result finished since you last asked,
and removes them from the queue. Each is a dictionary with:

- the output of each step, under the step's name;
- `_phase`, `_scope` and `_scope_level`, saying whether it is a tile result
  or a scoped one, and for which unit;
- `provenance`, one entry per step with its conda environment, its Python
  version, a fingerprint of every installed package, and the versions of
  the packages the step imported.

`engine.status(name)` counts the jobs that are pending, running, completed
and failed, and lists each failure with the step and the error message.

## What the engine does not do

- It does not decide what to image next. The acquisition software reads
  the results and decides.
- It does not store results. Steps that need files write them themselves,
  usually beside the images.
- It runs on one computer. It is not a cluster scheduler.
- It runs step files as they are, with your rights. Only run steps you trust.
