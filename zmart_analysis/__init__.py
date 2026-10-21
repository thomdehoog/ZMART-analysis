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
    pipeline.py       reads a pipeline, splits it into phases, tracks scopes,
                      keeps the job board
    priority_pool.py  the thread pool that starts the most urgent job first
    workers.py        the worker processes that run the steps, and their errors
    worker_script.py  runs inside a step's conda environment; imports nothing
                      from the engine but handover.py, so it works in any
                      environment
    handover.py       how one step's output reaches the next, through a folder
    record.py         the run record, run.jsonl
    conda.py          finds conda and the environments it knows
    environments.py   creates or removes a workflow's environments, for its
                      setup_env.py and clean_env.py
"""

from .engine import Engine
from .pipeline import ScopeError
from .workers import (
    StepExecutionError,
    WorkerCrashedError,
    WorkerError,
    WorkerSpawnError,
    WorkerTimeoutError,
)

__version__ = "0.5.0rc7"
__all__ = [
    "Engine",
    "WorkerError",
    "WorkerSpawnError",
    "WorkerCrashedError",
    "WorkerTimeoutError",
    "StepExecutionError",
    "ScopeError",
]
