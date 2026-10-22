"""
ZMART Analysis -- the engine that runs analysis pipelines.

A pipeline (YAML) lists steps; every step runs in a worker, a process in the
conda environment the step names. Steps may be scoped to run once per unit
of the sample (a compartment, a carrier) once the acquisition closes it.

API
---
    from zmart_analysis import Engine

    engine = Engine()
    engine.register("analysis", "path/to/a_scoped_pipeline.yaml",
                    "path/to/run")      # writes into path/to/run/analysis/analysis/
    job = engine.submit(pipeline="analysis", data=image,
                        scope={"carrier": 1, "compartment": 3})
    engine.submit(pipeline="analysis", data=last_image,
                  scope={"carrier": 1, "compartment": 3}, complete="compartment")
    board = engine.status("analysis")       # every job: step, scope, state, output
    engine.deregister("analysis")           # its jobs finish, its workers stop
    engine.shutdown()

Architecture
------------
    engine.py         the Engine: register, submit, status, deregister, shutdown
    pipeline.py       reads a pipeline into phases; a registered pipeline has
                      its units and its board
    units.py          which images belong together, and when a unit is complete
    board.py          the job board, the run record and the hand-over folders
    job_queue.py      the order in which waiting jobs start
    workers.py        a worker process that runs the steps, and its errors
    worker_pool.py    the workers of every environment, and who may use them
    worker_script.py  runs inside a step's conda environment; imports nothing
                      from the engine but handover.py, so it works in any
                      environment
    handover.py       how one step's output reaches the next, through a folder
    conda.py          finds conda and the environments it knows

The environments themselves are conda environment files that each workflow
keeps in its environments/ folder (conda env create -f <name>.yml).
"""

from .engine import Engine
from .units import ScopeError
from .workers import (
    StepExecutionError,
    WorkerCrashedError,
    WorkerError,
    WorkerSpawnError,
    WorkerTimeoutError,
)

__version__ = "0.6.0rc5"
__all__ = [
    "Engine",
    "WorkerError",
    "WorkerSpawnError",
    "WorkerCrashedError",
    "WorkerTimeoutError",
    "StepExecutionError",
    "ScopeError",
]
