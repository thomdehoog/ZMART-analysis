"""
Workers -- the separate processes that run the steps.

Every step runs in a worker: a Python process started inside the step's own
conda environment with ``conda run``, talking to the engine over a local
socket. A worker starts once and stays running, so a model it loaded for
the first image is still there for the next. This module holds one worker
(Worker), how it is started and spoken to, and the errors a worker can
raise (the pools that keep workers per environment are in worker_pool.py):

    WorkerError
    +-- WorkerSpawnError      the process failed to start or connect
    +-- WorkerCrashedError    the process died, or broke the protocol
    +-- WorkerTimeoutError    the step ran past its timeout and was killed
    +-- StepExecutionError    the step's run() raised

A worker is never stopped for being idle. It stops when the engine takes
its environment away (a pipeline was deregistered and no other needs it), when
the pipeline it runs for is cancelled, or at shutdown.
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

from .conda import CONDA_CMD, get_conda_info

logger = logging.getLogger(__name__)


class WorkerError(Exception):
    """Base exception for all worker subprocess errors.

    ``step`` names the step it happened in, when one was running.
    """

    def __init__(self, message, step=None):
        super().__init__(message)
        self.step = step


class WorkerSpawnError(WorkerError):
    """Worker subprocess failed to start or connect back."""


class WorkerCrashedError(WorkerError):
    """The worker is unusable: its process died, or it broke the protocol."""


class WorkerTimeoutError(WorkerError):
    """Step exceeded its execution timeout; the worker was killed."""


class StepExecutionError(WorkerError):
    """Step's run() raised an exception inside the worker subprocess."""

    def __init__(self, message, remote_traceback=None, step=None):
        super().__init__(message, step)
        self.remote_traceback = remote_traceback


ENGINE_DIR = Path(__file__).resolve().parent
WORKER_SCRIPT = ENGINE_DIR / "worker_script.py"

#: How messages to and from a worker are encoded; the worker script keeps
#: the same number. Protocol 5 is read by every Python from 3.8 on, and it
#: carries a large image in one piece rather than the slow, chunked form of
#: the old protocol 2.
PICKLE_PROTOCOL = 5

#: How long a caller waits on a worker's pipe before it looks whether the
#: worker was killed meanwhile (see Worker.answer_arrives).
WAIT_TURN_S = 0.5

# Maximum bytes of stderr to retain for crash diagnostics.
STDERR_BUFFER = 8192

#: One spawn at a time, for the whole process. `conda run` on Windows writes
#: its activation to a temp file whose name is not unique per invocation, so
#: concurrent spawns from one parent corrupt each other and never connect.
spawn_turn = threading.Lock()


def label(environment):
    """An environment's name in messages; the engine's own has none."""
    return environment or "orchestrator"


#: How long one accept() turn waits before checking for a shutdown, seconds.
ACCEPT_TURN_S = 0.25


def the_python_of(environment):
    """How the interpreter of a step's conda environment is reached.

    `conda run` activates the environment and starts the interpreter as its
    own child, so the worker is a grandchild of the engine, and is killed
    as a tree (see Worker.kill_tree).

    The interpreter is named by its path, not as `python`: on Windows,
    `conda run` leaves an interpreter that already stands first on PATH
    ahead of the environment's own, so a process started from another
    environment's shell ran every step in that shell's interpreter and
    failed on the first import the step needed. The path comes from
    `conda info`, read once.
    """
    prefix = the_prefix_of(environment)
    python = "python" if prefix is None else str(the_interpreter_in(prefix))
    return [CONDA_CMD, "run", "-n", environment, python]


environment_prefixes = None


def the_prefix_of(environment):
    """Where a named environment lives, or None when conda does not list it."""
    global environment_prefixes
    if environment_prefixes is None:
        try:
            environment_prefixes = {
                Path(env).name: Path(env) for env in get_conda_info().get("envs", [])
            }
        except Exception as why:  # noqa: BLE001 -- conda unreachable: fall back to the name
            logger.warning(
                "could not list conda environments (%s); naming the interpreter as 'python'", why
            )
            environment_prefixes = {}
    return environment_prefixes.get(environment)


def the_interpreter_in(prefix):
    """The interpreter an environment keeps, where each platform keeps it."""
    windows = prefix / "python.exe"
    return windows if windows.exists() else prefix / "bin" / "python"


class StderrDrainer:
    """Background thread that reads stderr so the pipe never fills.

    Without draining, a worker that logs errors to stderr can fill the
    OS pipe buffer (~4KB on Windows) and block permanently. This thread
    reads continuously and keeps the last STDERR_BUFFER bytes for crash
    diagnostics.
    """

    def __init__(self, stream):
        self.stream = stream
        self.buf = collections.deque(maxlen=STDERR_BUFFER)
        self.thread = threading.Thread(target=self.drain, daemon=True)
        self.thread.start()

    def drain(self):
        try:
            while True:
                chunk = self.stream.read(1024)
                if not chunk:
                    break
                self.buf.extend(chunk)
        except (ValueError, OSError):
            pass

    def get_output(self):
        """Return retained stderr as a string."""
        return bytes(self.buf).decode("utf-8", errors="replace")

    def close(self):
        """Close the stream (unblocks the drain thread)."""
        try:
            self.stream.close()
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
    connect_timeout : float
        Seconds to wait for the subprocess to connect back.
    """

    def __init__(self, environment=None, connect_timeout=60.0):
        self.environment = environment
        self.connect_timeout = connect_timeout

        self.process = None
        self.conn = None
        self.listener = None
        self.stderr_drainer = None
        self.current_step = None
        #: Set by :meth:`shutdown` with ``now``. A killed worker stays down,
        #: even when the kill landed before its spawn.
        self.closed = False

    def ensure_running(self):
        """Spawn the subprocess if not already running."""
        if self.process is not None and self.process.poll() is None:
            return
        self.refuse_if_put_down()

        self.cleanup()

        authkey = os.urandom(32)
        self.listener = Listener(("localhost", 0), authkey=authkey)
        port = self.listener.address[1]
        # The accept below waits in short turns, checking between them
        # whether a shutdown landed, so the worker's door can be closed from
        # another thread. On Linux closing the listening socket does not
        # wake a thread blocked in accept(); it would sit there for the
        # whole connect_timeout, holding the spawn turn and stalling every
        # other spawn in the process.
        self.listener._listener._socket.settimeout(ACCEPT_TURN_S)

        python = [sys.executable] if self.environment is None else the_python_of(self.environment)
        cmd = python + [str(WORKER_SCRIPT)]

        # Pass the engine's own PID for orphan detection. The worker cannot
        # rely on os.getppid(): under `conda run` its direct parent is the
        # wrapper process, which outlives a crashed engine.
        cmd.extend(
            ["--port", str(port), "--authkey", authkey.hex(), "--parent-pid", str(os.getpid())]
        )

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"

        # The worker is killed as a tree (see kill_tree): under `conda run`
        # the process started here is a wrapper, and the interpreter doing
        # the work is its child. Windows kills by tree from the wrapper's
        # pid; POSIX needs the wrapper to lead a session of its own.
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True

        env_label = label(self.environment)
        logger.debug("Worker spawning: env=%s, port=%d", env_label, port)

        # Spawn-to-connect under the one turn: the conda activation is what
        # races, and it runs in the child between Popen and the connect back,
        # so the turn is held until the worker is on the line.
        with spawn_turn:
            try:
                self.process = subprocess.Popen(
                    cmd,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    **kwargs,
                )
            except Exception as e:
                logger.error("Worker spawn failed for env=%s: %s", env_label, e)
                self.cleanup()
                raise WorkerSpawnError(f"Failed to start worker for '{env_label}': {e}") from e

            logger.debug("Worker process started: pid=%d, env=%s", self.process.pid, env_label)
            self.refuse_if_put_down()

            self.stderr_drainer = StderrDrainer(self.process.stderr)

            try:
                self.conn = self.accept_in_turns()
            except Exception as e:
                # A shutdown that landed during the spawn is reported as
                # that, not as a worker that could not connect.
                self.refuse_if_put_down()
                stderr = self.stderr_drainer.get_output() if self.stderr_drainer else ""
                logger.error(
                    "Worker connect failed: pid=%d, env=%s, stderr=%s",
                    self.process.pid,
                    env_label,
                    stderr[:500],
                )
                self.cleanup()
                raise WorkerSpawnError(
                    f"Worker for '{env_label}' failed to connect "
                    f"within {self.connect_timeout}s. stderr: {stderr}"
                ) from e

        self.refuse_if_put_down()
        logger.info("Worker ready: pid=%d, env=%s", self.process.pid, env_label)

    def accept_in_turns(self):
        """Wait for the worker to connect, a short turn at a time.

        Between turns a shutdown that landed is honoured at once, and a
        worker process that already died is reported without waiting out
        the rest of the connect timeout.
        """
        deadline = time.monotonic() + self.connect_timeout
        while True:
            try:
                return self.listener.accept()
            except TimeoutError:
                pass
            self.refuse_if_put_down()
            if self.process is not None and self.process.poll() is not None:
                raise OSError(
                    f"worker process exited with code {self.process.returncode} before connecting"
                )
            if time.monotonic() >= deadline:
                raise TimeoutError(f"no connection within {self.connect_timeout}s")

    def refuse_if_put_down(self):
        """A shutdown that landed at any point of the spawn wins.

        Checked before the spawn, right after it, and once the worker is on
        the line: whatever came up in between is killed again, and the
        caller is told the worker is gone.
        """
        if not self.closed:
            return
        self.cleanup()
        raise WorkerCrashedError(f"Worker for '{label(self.environment)}' was killed")

    def execute(self, steps, pipeline_data, timeout=300.0, files=None, report=None):
        """
        Run a chain of steps in the worker, and block until its output.

        Parameters
        ----------
        steps : list of (str, dict)
            The chain: each step's file and its parameters from the YAML,
            run one after another, the output of one the data of the next.
        pipeline_data : dict
            The data for the first step.
        timeout : float or None
            Seconds each step may take; it starts again at every step.
        files : dict, optional
            How the data goes through folders (see worker_script.py).
        report : callable, optional
            Called as ``report("start", name)`` before each step and
            ``report("done", record)`` after it.

        Raises
        ------
        StepExecutionError
            A step's run() raised an exception.
        WorkerTimeoutError
            A step exceeded `timeout` and the worker was killed.
        WorkerCrashedError
            The worker process died during the chain.
        Each names the step it happened in as ``.step``.
        """
        self.ensure_running()
        pid = self.process.pid
        env_label = label(self.environment)
        steps = [(str(path), params) for path, params in steps]
        self.current_step = Path(steps[0][0]).stem

        message = (steps, pipeline_data, files or {})
        try:
            data = pickle.dumps(message, protocol=PICKLE_PROTOCOL)
            logger.debug("Worker execute: sending %d bytes to pid=%d", len(data), pid)
            self.conn.send_bytes(data)
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            logger.error("Worker send failed: pid=%d, step=%s: %s", pid, self.current_step, e)
            self.cleanup()
            raise WorkerCrashedError(
                f"Worker for '{env_label}' lost connection: {e}", self.current_step
            ) from e

        # The connection is held here: a kill from another thread drops
        # self.conn while this one still waits on it.
        conn = self.conn
        while True:
            step = self.current_step
            try:
                if not self.answer_arrives(conn, timeout):
                    logger.error(
                        "Worker timed out: pid=%d, step=%s, timeout=%.0fs", pid, step, timeout
                    )
                    self.cleanup()
                    raise WorkerTimeoutError(
                        f"Worker for '{env_label}' timed out after {timeout}s in {step}", step
                    )
                kind, payload = pickle.loads(conn.recv_bytes())
            except (EOFError, ConnectionResetError, OSError) as e:
                stderr = self.stderr_drainer.get_output() if self.stderr_drainer else ""
                logger.error("Worker crashed: pid=%d, step=%s, stderr=%s", pid, step, stderr[:500])
                self.cleanup()
                raise WorkerCrashedError(
                    f"Worker for '{env_label}' crashed. stderr: {stderr}", step
                ) from e

            if kind == "start":
                # The next step: the timeout starts again.
                self.current_step = payload
                if report is not None:
                    report("start", payload)
            elif kind == "done":
                if report is not None:
                    report("done", payload)
            elif kind == "ok":
                self.current_step = None
                return payload
            elif kind == "error":
                self.current_step = None
                logger.warning("Step error: pid=%d, step=%s", pid, payload.get("step"))
                raise StepExecutionError(
                    payload.get("message", "Unknown error"),
                    remote_traceback=payload.get("traceback"),
                    step=payload.get("step"),
                )
            else:
                raise WorkerCrashedError(f"Worker sent an unknown reply: {kind!r}", step)

    def answer_arrives(self, conn, timeout):
        """Wait for the step's answer; False when *timeout* passes first.

        Waits in short turns and looks between them whether the worker was
        killed. On macOS a thread waiting on a pipe is not woken when another
        thread closes it, so a kill alone would leave this caller waiting.
        A killed worker raises OSError here, as a broken pipe does.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            turn = WAIT_TURN_S
            if deadline is not None:
                turn = min(turn, max(0.0, deadline - time.monotonic()))
            if conn.poll(turn):
                return True
            if self.closed:
                raise OSError("the worker was killed")
            if deadline is not None and time.monotonic() >= deadline:
                return False

    def is_alive(self):
        """Check if the worker process is still running."""
        return self.process is not None and self.process.poll() is None

    @property
    def status(self):
        """Current worker state for observability."""
        if not self.is_alive():
            state = "stopped"
        elif self.current_step:
            state = "busy"
        else:
            state = "idle"
        env_label = label(self.environment)
        return {
            "env": env_label,
            "state": state,
            "current_step": self.current_step,
            "pid": self.process.pid if self.process else None,
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
        self.closed = self.closed or now
        pid = self.process.pid if self.process else None
        if pid:
            env_label = label(self.environment)
            logger.debug("Worker shutdown: pid=%d, env=%s, now=%s", pid, env_label, now)

        if self.conn is not None and not now:
            try:
                self.conn.send_bytes(pickle.dumps(None, protocol=PICKLE_PROTOCOL))
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        if self.process is not None:
            try:
                self.process.wait(timeout=0.0 if now else 5.0)
            except subprocess.TimeoutExpired:
                logger.warning("Worker killed: pid=%d", pid)
                self.kill_tree()

        self.cleanup()

    def kill_tree(self):
        """Kill the worker process and everything under it.

        Under `conda run` the process this object holds is the wrapper, and
        the interpreter doing the work is its child; killing the wrapper
        alone would leave that child running.
        """
        process = self.process
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

    def cleanup(self):
        """Close connection, listener, drainer, and process handles."""
        self.current_step = None

        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

        if self.listener is not None:
            try:
                self.listener.close()
            except Exception:
                pass
            self.listener = None

        if self.stderr_drainer is not None:
            self.stderr_drainer.close()
            self.stderr_drainer = None

        if self.process is not None:
            self.kill_tree()
            self.process = None

    def __repr__(self):
        env_label = label(self.environment)
        return f"Worker(env={env_label!r}, alive={self.is_alive()})"
