"""
WorkerPool — Resource management with GPU slot and priority scheduling.

Routes step execution to the appropriate worker based on device type and
isolation setting. Two resource classes are managed independently:

CPU workers
-----------
Under minimal isolation: one persistent worker per environment. Multiple
steps in the same environment share a worker (no serialization). A lock
per worker prevents concurrent execute() calls.

Under maximal isolation: oneshot workers — spawn, run, exit. Every step
gets its own subprocess regardless of environment sharing.

GPU slot
--------
Only one GPU worker can be alive at a time (VRAM constraint). All GPU work
goes through a PriorityQueue serviced by a dedicated background thread.
Priority ordering ensures high-priority runs get GPU access first. Same-
environment work is batched naturally — the GPU worker stays alive as long
as the next item in the queue uses the same environment.

Under maximal isolation, each GPU step spawns a fresh worker (shutdown +
respawn per step). Under minimal, the GPU worker persists across calls and
only switches when the environment changes.

Reaper
------
A daemon thread runs every 30s and shuts down CPU workers that have been
idle longer than idle_timeout. GPU workers are not reaped — they are managed
exclusively by the GPU executor thread.

Thread safety
-------------
- _cpu_pool_lock protects the CPU worker/lock dicts during creation.
- _cpu_locks[env] protects individual workers from concurrent execute().
- GPU work is serialized through the single GPU executor thread.
"""

import concurrent.futures
import logging
import queue
import threading
import time

from ._worker import Worker
from ._errors import WorkerError

logger = logging.getLogger(__name__)


class WorkerPool:
    """
    Pool of workers with GPU slot management and priority scheduling.

    Parameters
    ----------
    idle_timeout : float
        Seconds before idle CPU workers are shut down (default: 300).
    connect_timeout : float
        Seconds to wait for a new worker to connect (default: 60).
    """

    def __init__(self, idle_timeout=300.0, connect_timeout=60.0):
        self.idle_timeout = idle_timeout
        self.connect_timeout = connect_timeout

        # CPU workers: env -> Worker, env -> Lock
        self._cpu_workers = {}
        self._cpu_locks = {}
        self._cpu_pool_lock = threading.Lock()

        # GPU: single worker + priority queue + executor thread
        self._gpu_worker = None
        self._gpu_queue = queue.PriorityQueue()
        self._gpu_thread = None
        self._gpu_thread_lock = threading.Lock()

        # Shared state
        self._shutdown_event = threading.Event()
        self._submission_counter = 0
        self._counter_lock = threading.Lock()
        self._reaper = None

    def execute(self, environment, device, step_path, pipeline_data, params,
                priority=0, timeout=300.0, isolation="minimal"):
        """
        Execute a step. Routes to CPU or GPU based on device type.

        Parameters
        ----------
        environment : str
            Conda environment for the worker.
        device : str
            "gpu" or "cpu".
        step_path : str
            Path to the step .py file.
        pipeline_data : dict
            Data to pass to the step.
        params : dict
            Step parameters from YAML.
        priority : int
            Priority level (higher = more urgent). Default 0.
        timeout : float
            Seconds to wait for step completion.
        isolation : str
            "minimal" or "maximal".
        """
        if device == "gpu":
            return self._execute_gpu(
                environment, step_path, pipeline_data, params,
                priority, timeout, isolation,
            )
        elif isolation == "maximal":
            return self._execute_oneshot(
                environment, step_path, pipeline_data, params, timeout,
            )
        else:
            self._ensure_reaper()
            return self._execute_cpu(
                environment, step_path, pipeline_data, params, timeout,
            )

    # ── CPU (minimal isolation) ──────────────────────────────────

    def _execute_cpu(self, environment, step_path, pipeline_data,
                     params, timeout):
        """Execute via a persistent per-environment CPU worker."""
        with self._cpu_pool_lock:
            if environment not in self._cpu_workers:
                logger.info("Pool: creating CPU worker for env=%s", environment)
                self._cpu_workers[environment] = Worker(
                    environment=environment,
                    device="cpu",
                    idle_timeout=self.idle_timeout,
                    connect_timeout=self.connect_timeout,
                )
                self._cpu_locks[environment] = threading.Lock()
            worker = self._cpu_workers[environment]
            lock = self._cpu_locks[environment]

        with lock:
            return worker.execute(step_path, pipeline_data, params,
                                  timeout=timeout)

    # ── CPU (maximal isolation) ──────────────────────────────────

    def _execute_oneshot(self, environment, step_path, pipeline_data,
                         params, timeout):
        """Execute via a oneshot worker (spawn, run, exit)."""
        logger.debug("Pool: oneshot worker for env=%s, step=%s",
                     environment, step_path)
        worker = Worker(
            environment=environment,
            device="cpu",
            oneshot=True,
            connect_timeout=self.connect_timeout,
        )
        try:
            return worker.execute(step_path, pipeline_data, params,
                                  timeout=timeout)
        finally:
            worker.shutdown()

    # ── GPU ──────────────────────────────────────────────────────

    def _execute_gpu(self, environment, step_path, pipeline_data,
                     params, priority, timeout, isolation):
        """Submit GPU work to the priority queue and wait for result."""
        self._ensure_gpu_thread()

        future = concurrent.futures.Future()
        with self._counter_lock:
            order = self._submission_counter
            self._submission_counter += 1

        # Negate priority so higher values are dequeued first
        self._gpu_queue.put((
            -priority, order, environment, step_path,
            pipeline_data, params, timeout, isolation, future,
        ))
        logger.debug("Pool: GPU work queued (priority=%d, order=%d, env=%s)",
                     priority, order, environment)

        try:
            return future.result(timeout=timeout + 60)
        except concurrent.futures.TimeoutError:
            raise WorkerError(
                f"GPU step timed out waiting in queue + execution "
                f"(timeout={timeout}s + 60s margin)"
            )

    def _ensure_gpu_thread(self):
        """Start the GPU executor thread on first GPU use."""
        with self._gpu_thread_lock:
            if self._gpu_thread is None or not self._gpu_thread.is_alive():
                logger.debug("Pool: starting GPU executor thread")
                self._gpu_thread = threading.Thread(
                    target=self._gpu_executor_loop, daemon=True,
                )
                self._gpu_thread.start()

    def _gpu_executor_loop(self):
        """Background thread: drain GPU queue, one item at a time."""
        while not self._shutdown_event.is_set():
            try:
                item = self._gpu_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            (neg_priority, order, env, step_path, pipeline_data,
             params, timeout, isolation, future) = item

            try:
                if isolation == "maximal":
                    result = self._gpu_execute_oneshot(
                        env, step_path, pipeline_data, params, timeout,
                    )
                else:
                    result = self._gpu_execute_persistent(
                        env, step_path, pipeline_data, params, timeout,
                    )
                future.set_result(result)
            except Exception as e:
                future.set_exception(e)

    def _gpu_execute_persistent(self, environment, step_path,
                                pipeline_data, params, timeout):
        """Execute on the persistent GPU worker, switching env if needed."""
        if (self._gpu_worker is None
                or self._gpu_worker.environment != environment
                or not self._gpu_worker.is_alive()):
            if self._gpu_worker is not None:
                logger.info("Pool: GPU switching %s -> %s",
                            self._gpu_worker.environment, environment)
                self._gpu_worker.shutdown()
            self._gpu_worker = Worker(
                environment=environment,
                device="gpu",
                idle_timeout=self.idle_timeout,
                connect_timeout=self.connect_timeout,
            )

        return self._gpu_worker.execute(step_path, pipeline_data, params,
                                        timeout=timeout)

    def _gpu_execute_oneshot(self, environment, step_path,
                             pipeline_data, params, timeout):
        """Execute on a fresh GPU worker (maximal isolation)."""
        if self._gpu_worker is not None:
            self._gpu_worker.shutdown()
            self._gpu_worker = None

        worker = Worker(
            environment=environment,
            device="gpu",
            oneshot=True,
            connect_timeout=self.connect_timeout,
        )
        try:
            return worker.execute(step_path, pipeline_data, params,
                                  timeout=timeout)
        finally:
            worker.shutdown()

    # ── Reaper ───────────────────────────────────────────────────

    def _ensure_reaper(self):
        """Start the reaper thread on first persistent CPU worker use."""
        with self._cpu_pool_lock:
            if self._reaper is None:
                logger.debug("Pool: starting reaper (idle_timeout=%.0fs)",
                             self.idle_timeout)
                self._reaper = threading.Thread(
                    target=self._reaper_loop, daemon=True,
                )
                self._reaper.start()

    def _reaper_loop(self):
        """Background thread: shut down idle CPU workers every 30s."""
        while not self._shutdown_event.wait(timeout=30.0):
            self._reap_idle()

    def _reap_idle(self):
        """Shut down CPU workers that exceed idle_timeout."""
        now = time.monotonic()
        to_shutdown = []

        with self._cpu_pool_lock:
            for env, worker in list(self._cpu_workers.items()):
                if (worker.is_idle(now)
                        and not self._cpu_locks[env].locked()):
                    to_shutdown.append(self._cpu_workers.pop(env))
                    self._cpu_locks.pop(env)

        if to_shutdown:
            logger.info("Pool: reaping %d idle CPU worker(s)",
                        len(to_shutdown))
        for worker in to_shutdown:
            worker.shutdown()

    # ── Status & shutdown ────────────────────────────────────────

    @property
    def status(self):
        """Current pool state for observability."""
        workers = []
        with self._cpu_pool_lock:
            for worker in self._cpu_workers.values():
                if worker.is_alive():
                    workers.append(worker.status)
        if self._gpu_worker and self._gpu_worker.is_alive():
            workers.append(self._gpu_worker.status)

        return {
            "workers": workers,
            "gpu_queue_depth": self._gpu_queue.qsize(),
        }

    def shutdown_all(self):
        """Shut down all workers and stop background threads."""
        self._shutdown_event.set()

        # CPU workers
        with self._cpu_pool_lock:
            n = len(self._cpu_workers)
            if n:
                logger.info("Pool: shutting down %d CPU worker(s)", n)
            for worker in self._cpu_workers.values():
                worker.shutdown()
            self._cpu_workers.clear()
            self._cpu_locks.clear()

        # GPU worker
        if self._gpu_worker is not None:
            logger.info("Pool: shutting down GPU worker")
            self._gpu_worker.shutdown()
            self._gpu_worker = None

        # Drain GPU queue, cancel pending futures
        while not self._gpu_queue.empty():
            try:
                item = self._gpu_queue.get_nowait()
                future = item[-1]
                future.set_exception(
                    WorkerError("Pool shut down while GPU work was pending"))
            except queue.Empty:
                break

        logger.debug("Pool: shutdown complete")

    def __repr__(self):
        with self._cpu_pool_lock:
            cpu_alive = sum(1 for w in self._cpu_workers.values()
                            if w.is_alive())
        gpu_alive = (self._gpu_worker is not None
                     and self._gpu_worker.is_alive())
        return (f"WorkerPool(cpu={cpu_alive}, gpu={'alive' if gpu_alive else 'none'}, "
                f"gpu_queue={self._gpu_queue.qsize()})")
