"""
Exception hierarchy for the pipeline engine.

All worker-related exceptions inherit from WorkerError, so callers can
catch the base class for any subprocess issue, or catch specific types:

    WorkerError (base)
    ├── WorkerSpawnError      — subprocess failed to start or connect
    ├── WorkerCrashedError    — subprocess died during execution
    └── StepExecutionError    — step's run() raised an exception
                                (includes .remote_traceback from subprocess)

Note: local (in-process) steps propagate their original exception type
directly — they do NOT wrap in StepExecutionError. This is an important
behavioral difference when writing error handling code.
"""


class WorkerError(Exception):
    """Base exception for worker errors."""


class WorkerSpawnError(WorkerError):
    """Worker subprocess failed to start or connect."""


class WorkerCrashedError(WorkerError):
    """Worker process died unexpectedly during execution."""


class StepExecutionError(WorkerError):
    """Step's run() raised an exception inside the worker."""

    def __init__(self, message, remote_traceback=None):
        super().__init__(message)
        self.remote_traceback = remote_traceback
