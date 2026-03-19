"""Pipeline Engine — unified orchestrator for sequential Python pipelines."""

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
