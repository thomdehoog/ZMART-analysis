"""
Workers -- the separate processes that run the steps.

Every step runs in a worker: a Python process started inside the step's own
conda environment with ``conda run``, talking to the engine over a local
socket. A worker starts once and stays running, so a model it loaded for
the first image is still there for the next. This module holds one worker
(Worker), the pool that keeps workers per environment (WorkerPool, with one
_EnvPool per environment and a semaphore per step for ``max_workers``), and
the errors a worker can raise:

    WorkerError
    +-- WorkerSpawnError      the process failed to start or connect
    +-- WorkerCrashedError    the process died, or broke the protocol
    +-- WorkerTimeoutError    the step ran past its timeout and was killed
    +-- StepExecutionError    the step's run() raised

Thread safety: the pools guard their dictionaries with locks and each
_EnvPool guards its idle and busy lists; a reaper thread stops idle workers.
"""

import collections
import logging
import os
import pickle
import signal
import subprocess
import sys
import threading
import time
from multiprocessing.connection import Listener
from pathlib import Path

from .conda_utils import CONDA_CMD, get_conda_info

logger = logging.getLogger(__name__)


class WorkerError(Exception):
    """Base exception for all worker subprocess errors."""


class WorkerSpawnError(WorkerError):
    """Worker subprocess failed to start or connect back."""


class WorkerCrashedError(WorkerError):
    """The worker is unusable: its process died, or it broke the protocol."""


class WorkerTimeoutError(WorkerError):
    """Step exceeded its execution timeout; the worker was killed."""


class StepExecutionError(WorkerError):
    """Step's run() raised an exception inside the worker subprocess."""

    def __init__(self, message, remote_traceback=None):
        super().__init__(message)
        self.remote_traceback = remote_traceback


ENGINE_DIR = Path(__file__).resolve().parent
WORKER_SCRIPT = ENGINE_DIR / "worker_script.py"

# Maximum bytes of stderr to retain for crash diagnostics.
_STDERR_BUFFER = 8192

#: One spawn at a time, for the whole process. `conda run` on Windows writes
#: its activation to a temp file whose name is not unique per invocation, so
#: concurrent spawns from one parent corrupt each other and never connect.
_spawn_turn = threading.Lock()


def _label(environment):
    """An environment's name in messages; the engine's own has none."""
    return environment or "orchestrator"


#: How long one accept() turn waits before checking for a shutdown, seconds.
_ACCEPT_TURN_S = 0.25


def _the_python_of(environment):
    """How the interpreter of a step's conda environment is reached.

    `conda run` activates the environment and starts the interpreter as its
    own child, so the worker is a grandchild of the engine, and is killed
    as a tree (see Worker._kill_tree).

    The interpreter is named by its path, not as `python`: on Windows,
    `conda run` leaves an interpreter that already stands first on PATH
    ahead of the environment's own, so a process started from another
    environment's shell ran every step in that shell's interpreter and
    failed on the first import the step needed. The path comes from
    `conda info`, read once.
    """
    prefix = _the_prefix_of(environment)
    python = "python" if prefix is None else str(_the_interpreter_in(prefix))
    return [CONDA_CMD, "run", "-n", environment, python]


_prefixes = None


def _the_prefix_of(environment):
    """Where a named environment lives, or None when conda does not list it."""
    global _prefixes
    if _prefixes is None:
        try:
            _prefixes = {Path(env).name: Path(env) for env in get_conda_info().get("envs", [])}
        except Exception as why:  # noqa: BLE001 -- conda unreachable: fall back to the name
            logger.warning(
                "could not list conda environments (%s); naming the interpreter as 'python'", why
            )
            _prefixes = {}
    return _prefixes.get(environment)


def _the_interpreter_in(prefix):
    """The interpreter an environment keeps, where each platform keeps it."""
    windows = prefix / "python.exe"
    return windows if windows.exists() else prefix / "bin" / "python"


class _StderrDrainer:
    """Background thread that reads stderr so the pipe never fills.

    Without draining, a worker that logs errors to stderr can fill the
    OS pipe buffer (~4KB on Windows) and block permanently. This thread
    reads continuously and keeps the last _STDERR_BUFFER bytes for crash
    diagnostics.
    """

    def __init__(self, stream):
        self._stream = stream
        self._buf = collections.deque(maxlen=_STDERR_BUFFER)
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self):
        try:
            while True:
                chunk = self._stream.read(1024)
                if not chunk:
                    break
                self._buf.extend(chunk)
        except (ValueError, OSError):
            pass

    def get_output(self):
        """Return retained stderr as a string."""
        return bytes(self._buf).decode("utf-8", errors="replace")

    def close(self):
        """Close the stream (unblocks the drain thread)."""
        try:
            self._stream.close()
        except Exception:
            pass


class Worker:
    """
    Manages a subprocess for one conda environment.

    Parameters
    ----------
    environment : str or None
        Conda environment name. None means the orchestrator's own
        environment (uses sys.executable directly).
    idle_timeout : float or None
        Seconds of inactivity before eligible for reaper shutdown; None
        means never.
    connect_timeout : float
        Seconds to wait for the subprocess to connect back.
    """

    def __init__(self, environment=None, idle_timeout=300.0, connect_timeout=60.0):
        self.environment = environment
        self.idle_timeout = idle_timeout
        self.connect_timeout = connect_timeout

        self._process = None
        self._conn = None
        self._listener = None
        self._stderr_drainer = None
        self._last_active = time.monotonic()
        self._current_step = None
        #: Set by :meth:`shutdown` with ``now``. A killed worker stays down,
        #: even when the kill landed before its spawn.
        self._closed = False

    def ensure_running(self):
        """Spawn the subprocess if not already running."""
        if self._process is not None and self._process.poll() is None:
            return
        self._refuse_if_put_down()

        self._cleanup()

        authkey = os.urandom(32)
        self._listener = Listener(("localhost", 0), authkey=authkey)
        port = self._listener.address[1]
        # The accept below waits in short turns, checking between them
        # whether a shutdown landed, so the worker's door can be closed from
        # another thread. On Linux closing the listening socket does not
        # wake a thread blocked in accept(); it would sit there for the
        # whole connect_timeout, holding the spawn turn and stalling every
        # other spawn in the process.
        self._listener._listener._socket.settimeout(_ACCEPT_TURN_S)

        python = [sys.executable] if self.environment is None else _the_python_of(self.environment)
        cmd = python + [str(WORKER_SCRIPT)]

        # Pass the engine's own PID for orphan detection. The worker cannot
        # rely on os.getppid(): under `conda run` its direct parent is the
        # wrapper process, which outlives a crashed engine.
        cmd.extend(
            ["--port", str(port), "--authkey", authkey.hex(), "--parent-pid", str(os.getpid())]
        )

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"

        # The worker is killed as a tree (see _kill_tree): under `conda run`
        # the process started here is a wrapper, and the interpreter doing
        # the work is its child. Windows kills by tree from the wrapper's
        # pid; POSIX needs the wrapper to lead a session of its own.
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True

        env_label = _label(self.environment)
        logger.debug("Worker spawning: env=%s, port=%d", env_label, port)

        # Spawn-to-connect under the one turn: the conda activation is what
        # races, and it runs in the child between Popen and the connect back,
        # so the turn is held until the worker is on the line.
        with _spawn_turn:
            try:
                self._process = subprocess.Popen(
                    cmd,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    **kwargs,
                )
            except Exception as e:
                logger.error("Worker spawn failed for env=%s: %s", env_label, e)
                self._cleanup()
                raise WorkerSpawnError(f"Failed to start worker for '{env_label}': {e}") from e

            logger.debug("Worker process started: pid=%d, env=%s", self._process.pid, env_label)
            self._refuse_if_put_down()

            self._stderr_drainer = _StderrDrainer(self._process.stderr)

            try:
                self._conn = self._accept_in_turns()
            except Exception as e:
                # A shutdown that landed during the spawn is reported as
                # that, not as a worker that could not connect.
                self._refuse_if_put_down()
                stderr = self._stderr_drainer.get_output() if self._stderr_drainer else ""
                logger.error(
                    "Worker connect failed: pid=%d, env=%s, stderr=%s",
                    self._process.pid,
                    env_label,
                    stderr[:500],
                )
                self._cleanup()
                raise WorkerSpawnError(
                    f"Worker for '{env_label}' failed to connect "
                    f"within {self.connect_timeout}s. stderr: {stderr}"
                ) from e

        self._refuse_if_put_down()
        self._last_active = time.monotonic()
        logger.info("Worker ready: pid=%d, env=%s", self._process.pid, env_label)

    def _accept_in_turns(self):
        """Wait for the worker to connect, a short turn at a time.

        Between turns a shutdown that landed is honoured at once, and a
        worker process that already died is reported without waiting out
        the rest of the connect timeout.
        """
        deadline = time.monotonic() + self.connect_timeout
        while True:
            try:
                return self._listener.accept()
            except TimeoutError:
                pass
            self._refuse_if_put_down()
            if self._process is not None and self._process.poll() is not None:
                raise OSError(
                    f"worker process exited with code {self._process.returncode} before connecting"
                )
            if time.monotonic() >= deadline:
                raise TimeoutError(f"no connection within {self.connect_timeout}s")

    def _refuse_if_put_down(self):
        """A shutdown that landed at any point of the spawn wins.

        Checked before the spawn, right after it, and once the worker is on
        the line: whatever came up in between is killed again, and the
        caller is told the worker is gone.
        """
        if not self._closed:
            return
        self._cleanup()
        raise WorkerCrashedError(f"Worker for '{_label(self.environment)}' was killed")

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
        WorkerTimeoutError
            If the step exceeded `timeout` and the worker was killed.
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
            logger.debug(
                "Worker execute: sending %d bytes to pid=%d (step=%s)", len(data), pid, step_name
            )
            self._conn.send_bytes(data)
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            logger.error("Worker send failed: pid=%d, step=%s: %s", pid, step_name, e)
            self._cleanup()
            env_label = _label(self.environment)
            raise WorkerCrashedError(f"Worker for '{env_label}' lost connection: {e}") from e

        try:
            if not self._conn.poll(timeout=timeout):
                logger.error(
                    "Worker timed out: pid=%d, step=%s, timeout=%.0fs", pid, step_name, timeout
                )
                self._cleanup()
                env_label = _label(self.environment)
                raise WorkerTimeoutError(f"Worker for '{env_label}' timed out after {timeout}s")
            raw = self._conn.recv_bytes()
        except (EOFError, ConnectionResetError, OSError) as e:
            stderr = self._stderr_drainer.get_output() if self._stderr_drainer else ""
            logger.error("Worker crashed: pid=%d, step=%s, stderr=%s", pid, step_name, stderr[:500])
            self._cleanup()
            env_label = _label(self.environment)
            raise WorkerCrashedError(f"Worker for '{env_label}' crashed. stderr: {stderr}") from e

        elapsed = time.monotonic() - t0
        response = pickle.loads(raw)
        self._last_active = time.monotonic()
        self._current_step = None

        if not isinstance(response, tuple) or len(response) != 2:
            env_label = _label(self.environment)
            raise WorkerCrashedError(f"Worker for '{env_label}' sent invalid response")

        status, payload = response
        if status == "error":
            logger.warning("Step error: pid=%d, step=%s, elapsed=%.2fs", pid, step_name, elapsed)
            raise StepExecutionError(
                payload.get("message", "Unknown error"),
                remote_traceback=payload.get("traceback"),
            )
        if status != "ok":
            raise WorkerCrashedError(f"Worker sent unknown status: {status!r}")

        logger.debug("Worker execute done: pid=%d, step=%s, elapsed=%.2fs", pid, step_name, elapsed)
        return payload

    def is_idle(self, now=None):
        """True if worker has been idle longer than idle_timeout.

        Never, when there is no idle_timeout: the worker is kept for as long
        as its pool is.
        """
        if self.idle_timeout is None:
            return False
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
        env_label = _label(self.environment)
        return {
            "env": env_label,
            "state": state,
            "current_step": self._current_step,
            "pid": self._process.pid if self._process else None,
        }

    def shutdown(self, now=False):
        """Shut the worker subprocess down.

        Politely by default: the sentinel is sent, and an idle worker exits
        on it within a moment. With ``now`` the worker is killed at once,
        tree and all, without the sentinel or the wait: a step in flight
        never reads the sentinel. A caller blocked in :meth:`execute` on
        that worker is released with :class:`WorkerCrashedError`, because
        the pipe breaks when the process does.
        """
        # Killed now means killed for good; a polite shutdown leaves the
        # object able to spawn again.
        self._closed = self._closed or now
        pid = self._process.pid if self._process else None
        if pid:
            env_label = _label(self.environment)
            logger.debug("Worker shutdown: pid=%d, env=%s, now=%s", pid, env_label, now)

        if self._conn is not None and not now:
            try:
                self._conn.send_bytes(pickle.dumps(None, protocol=2))
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        if self._process is not None:
            try:
                self._process.wait(timeout=0.0 if now else 5.0)
            except subprocess.TimeoutExpired:
                logger.warning("Worker killed: pid=%d", pid)
                self._kill_tree()

        self._cleanup()

    def _kill_tree(self):
        """Kill the worker process and everything under it.

        Under `conda run` the process this object holds is the wrapper, and
        the interpreter doing the work is its child; killing the wrapper
        alone would leave that child running.
        """
        process = self._process
        if process is None or process.poll() is not None:
            return
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            logger.error("Worker would not die: pid=%d", process.pid)

    def _cleanup(self):
        """Close connection, listener, drainer, and process handles."""
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

        if self._stderr_drainer is not None:
            self._stderr_drainer.close()
            self._stderr_drainer = None

        if self._process is not None:
            self._kill_tree()
            self._process = None

    def __repr__(self):
        env_label = _label(self.environment)
        return f"Worker(env={env_label!r}, alive={self.is_alive()})"


class _EnvPool:
    """Worker pool for one conda environment.

    Tracks idle and busy workers. Creates new workers on demand.
    The pool grows as needed and shrinks via idle timeout reaping.
    """

    def __init__(self, environment, idle_timeout, connect_timeout):
        self.environment = environment
        self.idle_timeout = idle_timeout
        self.connect_timeout = connect_timeout
        self._idle = []
        self._busy = []
        self._lock = threading.Lock()
        self._closed = False

    def acquire(self):
        """Get an idle worker or create a new one. Marks it busy."""
        with self._lock:
            if self._closed:
                raise RuntimeError("WorkerPool has been shut down")
            # Try to reuse an idle worker
            while self._idle:
                worker = self._idle.pop()
                if worker.is_alive():
                    self._busy.append(worker)
                    return worker
                # Dead worker, discard silently
                worker.shutdown()

            # No idle worker available, create a new one
            worker = Worker(
                environment=self.environment,
                idle_timeout=self.idle_timeout,
                connect_timeout=self.connect_timeout,
            )
            self._busy.append(worker)
            return worker

    def release(self, worker):
        """Return a worker to the idle pool."""
        with self._lock:
            try:
                self._busy.remove(worker)
            except ValueError:
                pass
            if worker.is_alive() and not self._closed:
                self._idle.append(worker)

    def reap_idle(self):
        """Shut down workers that have been idle too long."""
        to_shutdown = []
        with self._lock:
            active = []
            for worker in self._idle:
                if worker.is_idle():
                    to_shutdown.append(worker)
                else:
                    active.append(worker)
            self._idle = active

        for worker in to_shutdown:
            worker.shutdown()

        if to_shutdown:
            env_label = _label(self.environment)
            logger.info("Reaped %d idle worker(s) of %s", len(to_shutdown), env_label)

    def shutdown_all(self, now=False):
        """Shut down all workers in this pool; with ``now``, at once."""
        with self._lock:
            self._closed = True
            all_workers = self._idle + self._busy
            self._idle.clear()
            self._busy.clear()

        for worker in all_workers:
            worker.shutdown(now=now)

    @property
    def status(self):
        """Current pool state."""
        workers = []
        with self._lock:
            for worker in self._busy + self._idle:
                if worker.is_alive():
                    workers.append(worker.status)
        return workers


class WorkerPool:
    """
    Pool of per-environment worker pools with per-step concurrency.

    Parameters
    ----------
    idle_timeout : float or None
        Seconds before idle workers are shut down (default: 300); None
        means they are never reaped.
    connect_timeout : float
        Seconds to wait for a new worker to connect (default: 60).
    """

    def __init__(self, idle_timeout=300.0, connect_timeout=60.0):
        self.idle_timeout = idle_timeout
        self.connect_timeout = connect_timeout

        self._env_pools = {}
        self._pool_lock = threading.Lock()

        self._step_semaphores = {}
        self._sem_lock = threading.Lock()

        self._shutdown_event = threading.Event()
        self._reaper = None
        self._closed = False

    def execute(self, environment, step_path, pipeline_data, params, max_workers=1, timeout=300.0):
        """
        Execute a step in a worker subprocess.

        Blocks until a worker is available (respecting per-step concurrency)
        and the step completes.

        Parameters
        ----------
        environment : str or None
            Conda environment. None = orchestrator's environment.
        step_path : str
            Path to the step .py file.
        pipeline_data : dict
            Data to pass to the step.
        params : dict
            Step parameters from YAML.
        max_workers : int
            Maximum concurrent instances of this step.
        timeout : float
            Seconds to wait for step completion.
        """
        sem = self._get_semaphore(step_path, max_workers)
        sem.acquire()
        try:
            pool = self._get_env_pool(environment)
            worker = pool.acquire()
            try:
                return worker.execute(step_path, pipeline_data, params, timeout=timeout)
            finally:
                pool.release(worker)
        finally:
            sem.release()

    def _get_env_pool(self, environment):
        """Get or create the pool for an environment."""
        with self._pool_lock:
            if self._closed:
                raise RuntimeError("WorkerPool has been shut down")
            if environment not in self._env_pools:
                env_label = _label(environment)
                logger.info("Creating env pool for %s", env_label)
                self._env_pools[environment] = _EnvPool(
                    environment,
                    self.idle_timeout,
                    self.connect_timeout,
                )
                self._ensure_reaper()
            return self._env_pools[environment]

    def _get_semaphore(self, step_path, max_workers):
        """Get or create a concurrency semaphore for a step at this width.

        Keyed by the width as well as the file: two pipelines that share a
        step file and ask for different widths each get their own, rather
        than whichever width was registered first.
        """
        key = (step_path, int(max_workers))
        with self._sem_lock:
            if key not in self._step_semaphores:
                self._step_semaphores[key] = threading.Semaphore(max_workers)
            return self._step_semaphores[key]

    # -- Reaper --------------------------------------------------------

    def _ensure_reaper(self):
        """Start the reaper thread on first pool creation."""
        if self._reaper is None:
            logger.debug("Starting reaper (idle_timeout=%s)", self.idle_timeout)
            self._reaper = threading.Thread(
                target=self._reaper_loop,
                daemon=True,
            )
            self._reaper.start()

    def _reaper_loop(self):
        """Background thread: reap idle workers every 30s."""
        while not self._shutdown_event.wait(timeout=30.0):
            with self._pool_lock:
                pools = list(self._env_pools.values())
            for pool in pools:
                pool.reap_idle()

    # -- Status & shutdown ---------------------------------------------

    @property
    def status(self):
        """Current pool state for observability."""
        workers = []
        with self._pool_lock:
            for pool in self._env_pools.values():
                workers.extend(pool.status)
        return {"workers": workers}

    def shutdown_all(self, now=False):
        """Shut down all workers and stop background threads.

        With ``now`` every worker is killed at once, busy or not, so a step
        in flight stops too.
        """
        self._shutdown_event.set()

        with self._pool_lock:
            self._closed = True
            pools = list(self._env_pools.values())
            n = len(pools)

        if n:
            logger.info("Shutting down %d env pool(s) (now=%s)", n, now)
        for pool in pools:
            pool.shutdown_all(now=now)

        logger.debug("WorkerPool shutdown complete")

    def __repr__(self):
        with self._pool_lock:
            n_envs = len(self._env_pools)
        return f"WorkerPool(envs={n_envs})"
