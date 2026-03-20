"""
Worker — Manages a subprocess for one conda environment.

v3 change: workers are per-environment, not per-step. A single worker can
execute any step file sent to it, with modules cached inside the subprocess
for warm-start performance. The step path is sent with each execute() call
rather than fixed at construction time.

Spawns worker_script.py in the target conda environment and communicates
via multiprocessing.connection (TCP sockets with pickle serialization).

Lifecycle
---------
1. ensure_running(): Allocate random port, spawn subprocess, wait for
   it to connect back. Uses authkey for secure handshake.
2. execute(step_path, data, params): Send work, wait for response.
3. shutdown(): Send None sentinel, wait for graceful exit. Escalates:
   wait 5s → SIGTERM → wait 2s → SIGKILL.

Two modes
---------
- persistent (oneshot=False): Subprocess stays alive between execute()
  calls, keeping imported modules warm in memory. Managed by WorkerPool.
- oneshot (oneshot=True): Subprocess processes one request and exits.
  Created and destroyed per call. No warmup benefit but no idle cost.

Connection protocol
-------------------
- Parent creates Listener on localhost:0 (random port)
- Subprocess receives port + authkey via CLI args
- Subprocess connects back as Client
- Messages: (step_path, pipeline_data, params) via pickle
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
    Manages a subprocess for one conda environment.

    Parameters
    ----------
    environment : str
        Conda environment name, or "local" for current Python.
    device : str
        "gpu" or "cpu". Used for status reporting; does not affect execution.
    oneshot : bool
        If True, subprocess exits after one execution.
    idle_timeout : float
        Seconds of inactivity before eligible for reaper shutdown.
    connect_timeout : float
        Seconds to wait for the subprocess to connect back.
    """

    def __init__(self, environment, device="cpu", oneshot=False,
                 idle_timeout=300.0, connect_timeout=60.0):
        self.environment = environment
        self.device = device
        self.oneshot = oneshot
        self.idle_timeout = idle_timeout
        self.connect_timeout = connect_timeout

        self._process = None
        self._conn = None
        self._listener = None
        self._last_active = time.monotonic()
        self._current_step = None

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

        cmd.extend(["--port", str(port), "--authkey", authkey.hex()])
        if self.oneshot:
            cmd.append("--oneshot")

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"

        # On Windows, isolate the worker in its own process group so that
        # terminating it never sends CTRL_C_EVENT to the parent console.
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        logger.debug("Worker spawning: env=%s, device=%s, oneshot=%s, port=%d",
                     self.environment, self.device, self.oneshot, port)

        try:
            self._process = subprocess.Popen(
                cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                **kwargs,
            )
        except Exception as e:
            logger.error("Worker spawn failed for env=%s: %s",
                         self.environment, e)
            self._cleanup()
            raise WorkerSpawnError(
                f"Failed to start worker for '{self.environment}': {e}"
            ) from e

        logger.debug("Worker process started: pid=%d, env=%s",
                     self._process.pid, self.environment)

        try:
            self._conn = self._listener.accept()
        except Exception as e:
            stderr = ""
            if self._process.poll() is not None:
                stderr = self._process.stderr.read().decode(
                    "utf-8", errors="replace")
            logger.error("Worker connect failed: pid=%d, env=%s, stderr=%s",
                         self._process.pid, self.environment, stderr[:500])
            self._cleanup()
            raise WorkerSpawnError(
                f"Worker for '{self.environment}' failed to connect "
                f"within {self.connect_timeout}s. stderr: {stderr}"
            ) from e

        self._last_active = time.monotonic()
        logger.info("Worker ready: pid=%d, env=%s, device=%s, oneshot=%s",
                     self._process.pid, self.environment,
                     self.device, self.oneshot)

    def execute(self, step_path, pipeline_data, params, timeout=300.0):
        """
        Send work to the worker and block until result.

        Parameters
        ----------
        step_path : str
            Path to the step .py file.
        pipeline_data : dict
            Data dict to pass to the step's run() function.
        params : dict
            Keyword arguments from the YAML config.
        timeout : float
            Seconds to wait for the step to complete.

        Raises
        ------
        StepExecutionError
            If the step's run() raised an exception.
        WorkerCrashedError
            If the worker process died during execution.
        """
        self.ensure_running()
        pid = self._process.pid
        step_name = Path(step_path).stem
        self._current_step = step_name
        t0 = time.monotonic()

        message = (str(step_path), pipeline_data, params)
        try:
            data = pickle.dumps(message, protocol=2)
            logger.debug("Worker execute: sending %d bytes to pid=%d (step=%s)",
                         len(data), pid, step_name)
            self._conn.send_bytes(data)
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            logger.error("Worker send failed: pid=%d, step=%s: %s",
                         pid, step_name, e)
            self._cleanup()
            raise WorkerCrashedError(
                f"Worker for '{self.environment}' lost connection: {e}"
            ) from e

        try:
            if not self._conn.poll(timeout=timeout):
                logger.error("Worker timed out: pid=%d, step=%s, timeout=%.0fs",
                             pid, step_name, timeout)
                self._cleanup()
                raise StepExecutionError(
                    f"Worker for '{self.environment}' timed out after {timeout}s"
                )
            raw = self._conn.recv_bytes()
        except (EOFError, ConnectionResetError, OSError) as e:
            stderr = ""
            if self._process and self._process.poll() is not None:
                stderr = self._process.stderr.read().decode(
                    "utf-8", errors="replace")
            logger.error("Worker crashed: pid=%d, step=%s, stderr=%s",
                         pid, step_name, stderr[:500])
            self._cleanup()
            raise WorkerCrashedError(
                f"Worker for '{self.environment}' crashed. stderr: {stderr}"
            ) from e

        elapsed = time.monotonic() - t0
        response = pickle.loads(raw)
        self._last_active = time.monotonic()
        self._current_step = None

        if not isinstance(response, tuple) or len(response) != 2:
            raise WorkerCrashedError(
                f"Worker for '{self.environment}' sent invalid response"
            )

        status, payload = response
        if status == "error":
            logger.warning("Step error: pid=%d, step=%s, elapsed=%.2fs",
                           pid, step_name, elapsed)
            raise StepExecutionError(
                payload.get("message", "Unknown error"),
                remote_traceback=payload.get("traceback"),
            )
        if status != "ok":
            raise WorkerCrashedError(
                f"Worker sent unknown status: {status!r}"
            )

        logger.debug("Worker execute done: pid=%d, step=%s, elapsed=%.2fs",
                     pid, step_name, elapsed)
        return payload

    def is_idle(self, now=None):
        """True if worker has been idle longer than idle_timeout."""
        if now is None:
            now = time.monotonic()
        return (now - self._last_active) > self.idle_timeout

    def is_alive(self):
        """Check if the worker process is still running."""
        return self._process is not None and self._process.poll() is None

    @property
    def status(self):
        """Current worker state for observability."""
        if not self.is_alive():
            state = "stopped"
        elif self._current_step:
            state = "busy"
        else:
            state = "idle"
        return {
            "env": self.environment,
            "device": self.device,
            "state": state,
            "current_step": self._current_step,
            "pid": self._process.pid if self._process else None,
        }

    def shutdown(self):
        """Gracefully shut down the worker subprocess."""
        pid = self._process.pid if self._process else None
        if pid:
            logger.debug("Worker shutdown: pid=%d, env=%s", pid,
                         self.environment)

        if self._conn is not None:
            try:
                self._conn.send_bytes(pickle.dumps(None, protocol=2))
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        if self._process is not None:
            try:
                self._process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                logger.warning("Worker terminating: pid=%d", pid)
                self._process.terminate()
                try:
                    self._process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    logger.warning("Worker killing: pid=%d", pid)
                    self._process.kill()
                    self._process.wait(timeout=1.0)

        self._cleanup()

    def _cleanup(self):
        """Close connection, listener, and process handles."""
        self._current_step = None

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
        return (f"Worker(env={self.environment!r}, device={self.device!r}, "
                f"alive={self.is_alive()})")
