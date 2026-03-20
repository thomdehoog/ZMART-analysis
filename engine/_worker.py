"""
Worker — Manages a subprocess for a single (environment, step) pair.

Spawns worker_script.py in the target conda environment and communicates
via multiprocessing.connection (TCP sockets with pickle serialization).

Lifecycle
---------
1. ensure_running(): Allocate random port, spawn subprocess, wait for
   it to connect back. Uses authkey for secure handshake.
2. execute(): Pickle (pipeline_data, params), send to worker, wait for
   response. Returns the step's result dict.
3. shutdown(): Send None sentinel, wait for graceful exit. Escalates:
   wait 5s → SIGTERM → wait 2s → SIGKILL.

Two modes
---------
- persistent (oneshot=False): Subprocess stays alive between execute()
  calls, keeping imported modules and ML models warm in memory.
  Managed by WorkerPool, reaped when idle.

- oneshot (oneshot=True): Subprocess processes one request and exits.
  Created and destroyed per call. No warmup benefit but no idle cost.

Connection protocol
-------------------
- Parent creates Listener on localhost:0 (random port)
- Subprocess receives port + authkey via CLI args
- Subprocess connects back as Client
- Messages are pickle-serialized bytes via send_bytes/recv_bytes
- Shutdown sentinel: None (pickled)

Error handling
--------------
- WorkerSpawnError: subprocess failed to start or connect
- WorkerCrashedError: subprocess died during execution
- StepExecutionError: step's run() raised (includes remote traceback)
"""

import logging
import os
import pickle
import subprocess
import sys
import time
from multiprocessing.connection import Listener
from pathlib import Path

from .conda_utils import CONDA_CMD
from ._errors import WorkerSpawnError, WorkerCrashedError, StepExecutionError

logger = logging.getLogger(__name__)

ENGINE_DIR = Path(__file__).resolve().parent
WORKER_SCRIPT = ENGINE_DIR / "worker_script.py"


class Worker:
    """
    Manages a single persistent subprocess for one (environment, step_path) pair.

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

    def __init__(self, environment, step_path, oneshot=False,
                 idle_timeout=300.0, connect_timeout=60.0):
        self.environment = environment
        self.step_path = str(step_path)
        self.oneshot = oneshot
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

        self._cleanup()

        authkey = os.urandom(32)
        self._listener = Listener(("localhost", 0), authkey=authkey)
        port = self._listener.address[1]
        self._listener._listener._socket.settimeout(self.connect_timeout)

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
        if self.oneshot:
            cmd.append("--oneshot")

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"

        step_name = Path(self.step_path).name
        logger.debug("Worker spawning: env=%s, step=%s, oneshot=%s, port=%d",
                     self.environment, step_name, self.oneshot, port)

        # On Windows, isolate the worker in its own process group so that
        # terminating it never sends CTRL_C_EVENT to the parent console.
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        try:
            self._process = subprocess.Popen(
                cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                **kwargs,
            )
        except Exception as e:
            logger.error("Worker spawn failed for env=%s, step=%s: %s",
                         self.environment, step_name, e)
            self._cleanup()
            raise WorkerSpawnError(
                f"Failed to start worker for '{self.environment}': {e}"
            ) from e

        logger.debug("Worker process started: pid=%d, env=%s, step=%s",
                     self._process.pid, self.environment, step_name)

        try:
            self._conn = self._listener.accept()
        except Exception as e:
            stderr = ""
            if self._process.poll() is not None:
                stderr = self._process.stderr.read().decode("utf-8", errors="replace")
            logger.error("Worker connect failed: pid=%d, env=%s, step=%s, "
                         "timeout=%.0fs, stderr=%s",
                         self._process.pid, self.environment, step_name,
                         self.connect_timeout, stderr[:500])
            self._cleanup()
            raise WorkerSpawnError(
                f"Worker for '{self.environment}' failed to connect "
                f"within {self.connect_timeout}s. stderr: {stderr}"
            ) from e

        self._last_active = time.monotonic()
        logger.info("Worker ready: pid=%d, env=%s, step=%s, oneshot=%s",
                     self._process.pid, self.environment, step_name, self.oneshot)

    def execute(self, pipeline_data, params, timeout=300.0):
        """
        Send work to the worker and block until result.

        Raises
        ------
        StepExecutionError
            If the step's run() raised an exception.
        WorkerCrashedError
            If the worker process died during execution.
        """
        self.ensure_running()
        pid = self._process.pid
        step_name = Path(self.step_path).name
        t0 = time.monotonic()

        message = (pipeline_data, params)
        try:
            data = pickle.dumps(message, protocol=2)
            logger.debug("Worker execute: sending %d bytes to pid=%d "
                         "(step=%s, timeout=%.0fs)",
                         len(data), pid, step_name, timeout)
            self._conn.send_bytes(data)
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            logger.error("Worker send failed: pid=%d, step=%s: %s",
                         pid, step_name, e)
            self._cleanup()
            raise WorkerCrashedError(
                f"Worker for '{self.environment}' connection lost while sending: {e}"
            ) from e

        try:
            if not self._conn.poll(timeout=timeout):
                elapsed = time.monotonic() - t0
                logger.error("Worker timed out: pid=%d, step=%s, "
                             "elapsed=%.2fs, timeout=%.0fs",
                             pid, step_name, elapsed, timeout)
                self._cleanup()
                raise StepExecutionError(
                    f"Worker for '{self.environment}' timed out after {timeout}s"
                )
            raw = self._conn.recv_bytes()
        except (EOFError, ConnectionResetError, OSError) as e:
            stderr = ""
            if self._process and self._process.poll() is not None:
                stderr = self._process.stderr.read().decode("utf-8", errors="replace")
            logger.error("Worker crashed during execution: pid=%d, step=%s, "
                         "stderr=%s", pid, step_name, stderr[:500])
            self._cleanup()
            raise WorkerCrashedError(
                f"Worker for '{self.environment}' crashed during execution. "
                f"stderr: {stderr}"
            ) from e

        elapsed = time.monotonic() - t0
        response = pickle.loads(raw)
        self._last_active = time.monotonic()

        if not isinstance(response, tuple) or len(response) != 2:
            logger.error("Worker sent invalid response: pid=%d, step=%s, "
                         "response_type=%s", pid, step_name, type(response).__name__)
            raise WorkerCrashedError(
                f"Worker for '{self.environment}' sent invalid response"
            )

        status, payload = response
        if status == "error":
            logger.warning("Worker step error: pid=%d, step=%s, "
                           "elapsed=%.2fs, error=%s",
                           pid, step_name, elapsed,
                           payload.get("message", "unknown"))
            raise StepExecutionError(
                payload.get("message", "Unknown error in worker"),
                remote_traceback=payload.get("traceback"),
            )
        if status != "ok":
            logger.error("Worker unknown status: pid=%d, step=%s, status=%r",
                         pid, step_name, status)
            raise WorkerCrashedError(
                f"Worker for '{self.environment}' sent unknown status: {status!r}"
            )

        logger.debug("Worker execute complete: pid=%d, step=%s, "
                     "elapsed=%.2fs, received=%d bytes",
                     pid, step_name, elapsed, len(raw))
        return payload

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
        pid = self._process.pid if self._process else None
        if pid:
            logger.debug("Worker shutdown starting: pid=%d, env=%s",
                         pid, self.environment)

        if self._conn is not None:
            try:
                self._conn.send_bytes(pickle.dumps(None, protocol=2))
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        if self._process is not None:
            try:
                self._process.wait(timeout=5.0)
                logger.debug("Worker exited gracefully: pid=%d", pid)
            except subprocess.TimeoutExpired:
                logger.warning("Worker did not exit in 5s, terminating: pid=%d",
                               pid)
                self._process.terminate()
                try:
                    self._process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    logger.warning("Worker did not terminate in 2s, "
                                   "killing: pid=%d", pid)
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
            if self._process.stderr:
                try:
                    self._process.stderr.close()
                except Exception:
                    pass
            if self._process.poll() is None:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None

    def __repr__(self):
        return (f"Worker(env={self.environment!r}, "
                f"step={Path(self.step_path).name!r}, "
                f"alive={self.is_alive()})")
