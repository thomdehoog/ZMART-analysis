"""
Pipeline Engine v3 — Unified orchestrator with scoped execution.

Runs YAML-defined workflows where each step is a Python function that can
execute locally or in an isolated conda environment. Supports scoped
triggering (spatial/temporal), priority scheduling, per-environment workers,
GPU slot management, and system-wide observability.

Quick start
-----------
Simple (one pipeline, no scopes):

    from engine import run_pipeline
    result = run_pipeline("pipeline.yaml", "my_run", {"input": "value"})

With runs and scopes:

    from engine import PipelineEngine
    engine = PipelineEngine()
    overview = engine.create_run("overview.yaml", priority="high")
    overview.submit("tile_1", data, spatial={"region": "R3"})
    overview.submit("tile_2", data, spatial={"region": "R3"})
    future = overview.scope_complete(spatial={"region": "R3"})
    result = future.result()
    engine.shutdown()

Architecture
------------
    _loader.py        AST-based METADATA extraction + exec-based module loading
    _run.py           Run with phases, scope tracking, result accumulation
    _pipeline.py      PipelineEngine orchestrator + step routing
    _pool.py          WorkerPool with GPU slot + priority queue
    _worker.py        Per-environment subprocess lifecycle
    worker_script.py  Runs inside target conda env (self-contained)
    _errors.py        WorkerError + ScopeError hierarchies
"""

from ._pipeline import PipelineEngine, run_pipeline
from ._run import Run
from ._errors import (
    WorkerError,
    WorkerSpawnError,
    WorkerCrashedError,
    StepExecutionError,
    ScopeError,
)

__version__ = "3.0.0"
__all__ = [
    "run_pipeline",
    "PipelineEngine",
    "Run",
    "WorkerError",
    "WorkerSpawnError",
    "WorkerCrashedError",
    "StepExecutionError",
    "ScopeError",
]
