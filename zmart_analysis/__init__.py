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
    engine.submit("analysis", image, scope={"carrier": 1, "compartment": 3})
    engine.submit("analysis", last_image, scope={"carrier": 1, "compartment": 3},
                  complete="compartment")
    results = engine.results("analysis")
    engine.shutdown()

Architecture
------------
    engine.py         the Engine: register, submit, status, results
    pipeline.py       reads a recipe, splits it into phases, tracks scopes
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
