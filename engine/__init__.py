"""
Pipeline Engine v4 -- Simplified orchestrator with scoped execution.

Runs YAML-defined workflows where every step executes in a worker subprocess.
Supports scoped triggering, per-step concurrency (max_workers), priority
scheduling, and system-wide observability.

API
---
    from engine import Engine

    engine = Engine()
    engine.register("overview", "overview_pipeline.yaml")
    engine.submit("overview", data, scope={"group": "R3"})
    engine.submit("overview", data, scope={"group": "R3"}, complete="group")
    results = engine.results("overview")
    engine.shutdown()

Architecture
------------
    engine.py         the Engine: register, submit, status, results
    pipeline.py       reads a recipe, splits it into phases, tracks scopes
    workers.py        the worker processes that run the steps, and their errors
    worker_script.py  runs inside a step's conda environment; imports nothing
                      from the engine, so it works in any environment
    conda_utils.py    finds conda, and creates or removes a workflow's
                      environments for its setup_env.py and clean_env.py
"""

from .engine import Engine
from .pipeline import ScopeError
from .workers import (
    WorkerError,
    WorkerSpawnError,
    WorkerCrashedError,
    WorkerTimeoutError,
    StepExecutionError,
)

__version__ = "4.0.0"
__all__ = [
    "Engine",
    "WorkerError",
    "WorkerSpawnError",
    "WorkerCrashedError",
    "WorkerTimeoutError",
    "StepExecutionError",
    "ScopeError",
]
