"""
Streaming Engine

A pipeline engine with persistent warm workers for real-time analysis.
Workers stay alive between invocations, eliminating cold-start overhead
for steps that load heavy libraries (PyTorch, Cellpose, etc.).
"""

from .engine import StreamingEngine
from .worker import StepExecutionError, WorkerCrashedError, WorkerSpawnError

__version__ = "0.1.0"
__all__ = [
    "StreamingEngine",
    "StepExecutionError",
    "WorkerCrashedError",
    "WorkerSpawnError",
]
