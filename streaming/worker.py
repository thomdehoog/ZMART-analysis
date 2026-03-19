"""
Worker — Manages one persistent subprocess for a (environment, step) pair.

Spawns the worker_script.py in the target conda environment and communicates
via multiprocessing.connection (TCP sockets with pickle serialization).
"""

import os
import pickle
import subprocess
import sys
import time
from multiprocessing.connection import Listener
from pathlib import Path

# Resolve paths once at import time
STREAMING_DIR = Path(__file__).resolve().parent
WORKER_SCRIPT = STREAMING_DIR / "worker_script.py"

# Import CONDA_CMD from the engine package (sibling directory)
ENGINE_DIR = STREAMING_DIR.parent / "engine"
sys.path.insert(0, str(ENGINE_DIR))
from conda_utils import CONDA_CMD
sys.path.pop(0)


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


class Worker:
    """
    Manages a single persistent subprocess for one (environment, step_path) pair.

    The subprocess stays alive between execute() calls, keeping imported modules
    (and ML models) warm in memory.

    Parameters
    ----------
    environment : str
        Conda environment name, or "local" for current Python.
    step_path : str
        Absolute path to the step .py file.
    idle_timeout : float
        Seconds of inactivity before this worker is eligible for shutdown.
    connect_timeout : float
        Seconds to wait for the worker subprocess to connect back.
    """

    def __init__(self, environment, step_path, idle_timeout=300.0,
                 connect_timeout=60.0):
        self.environment = environment
        self.step_path = str(step_path)
        self.idle_timeout = idle_timeout
        self.connect_timeout = connect_timeout

        self._process = None
        self._conn = None
        self._listener = None
        self._last_active = time.monotonic()

    def ensure_running(self):
        """Spawn the subprocess if not already running."""
        if self._process is not None and self._process.poll() is None:
            return

        # Clean up any stale state
        self._cleanup()

        # Create listener on a free port with shared authkey
        authkey = os.urandom(32)
        self._listener = Listener(("localhost", 0), authkey=authkey)
        port = self._listener.address[1]
        self._listener._listener._socket.settimeout(self.connect_timeout)

        # Build command
        if self.environment.lower() == "local":
            cmd = [sys.executable, str(WORKER_SCRIPT)]
        else:
            cmd = [CONDA_CMD, "run", "-n", self.environment,
                   "python", str(WORKER_SCRIPT)]

        cmd.extend([
            "--port", str(port),
            "--authkey", authkey.hex(),
            "--step", self.step_path,
        ])

        # Set environment
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"

        try:
            self._process = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except Exception as e:
            self._cleanup()
            raise WorkerSpawnError(
                f"Failed to start worker for '{self.environment}': {e}"
            ) from e

        # Wait for worker to connect back
        try:
            self._conn = self._listener.accept()
        except Exception as e:
            stderr = ""
            if self._process.poll() is not None:
                stderr = self._process.stderr.read().decode("utf-8", errors="replace")
            self._cleanup()
            raise WorkerSpawnError(
                f"Worker for '{self.environment}' failed to connect "
                f"within {self.connect_timeout}s. stderr: {stderr}"
            ) from e

        self._last_active = time.monotonic()

    def execute(self, pipeline_data, params, timeout=300.0):
        """
        Send work to the worker and block until result.

        Parameters
        ----------
        pipeline_data : dict
            Current pipeline data.
        params : dict
            Step parameters from YAML.
        timeout : float
            Seconds to wait for the step to complete.

        Returns
        -------
        dict
            Updated pipeline_data from the step.

        Raises
        ------
        StepExecutionError
            If the step's run() raised an exception.
        WorkerCrashedError
            If the worker process died during execution.
        """
        self.ensure_running()

        # Send work
        message = (pipeline_data, params)
        try:
            self._conn.send_bytes(pickle.dumps(message, protocol=2))
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            self._cleanup()
            raise WorkerCrashedError(
                f"Worker for '{self.environment}' connection lost while sending: {e}"
            ) from e

        # Wait for result
        try:
            if not self._conn.poll(timeout=timeout):
                self._cleanup()
                raise StepExecutionError(
                    f"Worker for '{self.environment}' timed out after {timeout}s"
                )
            raw = self._conn.recv_bytes()
        except (EOFError, ConnectionResetError, OSError) as e:
            stderr = ""
            if self._process and self._process.poll() is not None:
                stderr = self._process.stderr.read().decode("utf-8", errors="replace")
            self._cleanup()
            raise WorkerCrashedError(
                f"Worker for '{self.environment}' crashed during execution. "
                f"stderr: {stderr}"
            ) from e

        result = pickle.loads(raw)
        self._last_active = time.monotonic()

        # Check for remote error
        if isinstance(result, dict) and result.get("__error__"):
            raise StepExecutionError(
                result.get("message", "Unknown error in worker"),
                remote_traceback=result.get("traceback"),
            )

        return result

    def is_idle(self, now=None):
        """True if worker has been idle longer than idle_timeout."""
        if now is None:
            now = time.monotonic()
        return (now - self._last_active) > self.idle_timeout

    def is_alive(self):
        """Check if the worker process is still running."""
        return self._process is not None and self._process.poll() is None

    def shutdown(self):
        """Gracefully shut down the worker subprocess."""
        if self._conn is not None:
            try:
                self._conn.send_bytes(pickle.dumps(None, protocol=2))
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        if self._process is not None:
            try:
                self._process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self._process.terminate()
                try:
                    self._process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=1.0)

        self._cleanup()

    def _cleanup(self):
        """Close connection, listener, and process handles."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

        if self._listener is not None:
            try:
                self._listener.close()
            except Exception:
                pass
            self._listener = None

        if self._process is not None:
            if self._process.poll() is None:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None

    def __repr__(self):
        alive = self.is_alive()
        return (f"Worker(env={self.environment!r}, step={Path(self.step_path).name!r}, "
                f"alive={alive})")

    def __del__(self):
        self.shutdown()
