# zmart_analysis

The engine. It runs analysis pipelines, but never runs step code itself:
every step runs in a worker, a separate process in the step's own conda
environment.

```python
from zmart_analysis import Engine
```

## How a job flows

1. `Engine.register` reads the pipeline YAML and each step's `METADATA`,
   without running the step files.
2. `Engine.submit` puts an image job in the queue. Jobs start in order of
   their deadline, then of submission.
3. A worker for the step's environment takes the job. It starts the first
   time it is needed and stays running.
4. Each step writes its output to a folder. The next step reads it from
   there, so arrays never pass through the engine.
5. When a submit marks a unit complete (a compartment, a carrier), the
   scoped steps for that unit run once on the outputs of the level below.
6. The job board shows every job's state and, once done, its output.

## Modules

| File | What it does |
|---|---|
| `engine.py` | `Engine`: register, submit, read the board, deregister, shut down. |
| `pipeline.py` | Reads the YAML and splits it into phases at every `scope`. |
| `units.py` | Tracks which images belong to which unit, and when a unit is complete. |
| `board.py` | The job board, the run record, and the hand-over folders. |
| `job_queue.py` | The order in which waiting jobs start. |
| `worker_pool.py` | Keeps the workers per environment and limits how many run a step at once. |
| `workers.py` | One worker: how it starts with `conda run`, how the engine talks to it, and its errors. |
| `worker_script.py` | The script a worker runs inside the step's environment. |
| `handover.py` | Writes and reads a step's output: arrays as `.npy`, the rest as a pickle. |
| `conda.py` | Finds conda and its environments. |

`worker_script.py` and `handover.py` use only the standard library, so a
step can run in any Python version and any set of packages.

## Errors

`WorkerSpawnError`, `WorkerCrashedError` and `WorkerTimeoutError` (all a
`WorkerError`), `StepExecutionError` and `ScopeError` are exported with
`Engine`.

How to use the engine is in [docs part 1](../docs/1_use_the_engine/README.md).
