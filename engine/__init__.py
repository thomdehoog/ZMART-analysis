"""
Pipeline Engine — unified orchestrator for sequential Python pipelines.

Runs YAML-defined workflows where each step is a Python function that can
execute locally or in an isolated conda environment via subprocess. Handles
environment switching, persistent warm workers for ML models, concurrency
control, and transparent data serialization across process boundaries.

Quick start
-----------
    from engine import run_pipeline

    result = run_pipeline("pipeline.yaml", "my_run", {"input_key": "value"})

For persistent engine with concurrent submissions:

    from engine import PipelineEngine

    with PipelineEngine() as engine:
        future = engine.submit("pipeline.yaml", "run_1", data)
        result = future.result()

Architecture
------------
    _loader.py        AST-based METADATA extraction + exec-based module loading
    _pipeline.py      PipelineEngine orchestrator (YAML → step dispatch)
    _pool.py          WorkerPool with per-step semaphores and idle reaper
    _worker.py        Worker subprocess lifecycle (spawn, connect, execute)
    worker_script.py  Runs inside target conda env (self-contained, no engine imports)
    _errors.py        WorkerError hierarchy (spawn, crash, step execution)
"""

from ._pipeline import PipelineEngine, run_pipeline
from ._errors import (
    WorkerError,
    WorkerSpawnError,
    WorkerCrashedError,
    StepExecutionError,
)

__version__ = "2.0.0"
__all__ = [
    "run_pipeline",
    "PipelineEngine",
    "WorkerError",
    "WorkerSpawnError",
    "WorkerCrashedError",
    "StepExecutionError",
]
