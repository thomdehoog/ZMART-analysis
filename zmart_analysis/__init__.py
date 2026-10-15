"""
ZMART Analysis -- the engine that runs analysis recipes.

A recipe (YAML) lists steps; every step runs in a worker, a process in the
conda environment the step names. Steps may be scoped to run once per unit
of the sample (a compartment, a carrier) once the acquisition closes it.

API
---
    from zmart_analysis import Engine

    engine = Engine()
    engine.register("analysis", "workflows/object_analysis/pipelines/object_analysis_scoped.yaml")
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
    pipeline.py       reads a recipe, splits it into phases, tracks scopes,
                      keeps the job board
    priority_pool.py  the thread pool that starts the most urgent job first
    workers.py        the worker processes that run the steps, and their errors
    worker_script.py  runs inside a step's conda environment; imports nothing
                      from the engine, so it works in any environment
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

__version__ = "1.0.0rc1"
__all__ = [
    "Engine",
    "WorkerError",
    "WorkerSpawnError",
    "WorkerCrashedError",
    "WorkerTimeoutError",
    "StepExecutionError",
    "ScopeError",
]
