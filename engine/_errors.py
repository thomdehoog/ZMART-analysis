"""
Exception hierarchy for the pipeline engine.

Two independent hierarchies cover all error cases:

    WorkerError (base for all subprocess issues)
    ├── WorkerSpawnError      — subprocess failed to start or connect
    ├── WorkerCrashedError    — subprocess died during execution
    └── StepExecutionError    — step's run() raised an exception
                                (includes .remote_traceback from subprocess)

    ScopeError                — invalid scope configuration or completion

Important behavioral note: local (in-process) steps propagate their original
exception type directly — they do NOT wrap in StepExecutionError. This means
catching StepExecutionError only covers isolated steps. Callers that need to
handle both local and isolated errors should catch the original type OR
StepExecutionError, or catch Exception as a catch-all.
"""


class WorkerError(Exception):
    """Base exception for all worker subprocess errors."""


class WorkerSpawnError(WorkerError):
    """Worker subprocess failed to start or connect back."""


class WorkerCrashedError(WorkerError):
    """Worker process died unexpectedly during execution."""


class StepExecutionError(WorkerError):
    """Step's run() raised an exception inside the worker subprocess."""

    def __init__(self, message, remote_traceback=None):
        super().__init__(message)
        self.remote_traceback = remote_traceback


class ScopeError(Exception):
    """Invalid scope configuration, missing results, or bad completion signal."""
