"""Exception classes for the pipeline engine."""


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
