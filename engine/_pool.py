"""
WorkerPool — Per-step dispatch with concurrency control.

Routes step execution to either persistent or oneshot workers based on the
step's METADATA. Enforces per-step concurrency limits via semaphores.

Worker types
------------
- Persistent (worker="persistent"): Subprocess stays alive between calls.
  Keeps imported modules and ML models warm in memory. Reused for repeated
  invocations of the same step. Reaped after idle_timeout seconds.

- Oneshot (worker="subprocess", the default): Subprocess spawned for each
  call, exits after one execution. No memory overhead between calls.

Concurrency control
-------------------
Each (environment, step_path) pair gets a Semaphore(max_workers).
If max_workers=1 (default), calls to the same step serialize.
If max_workers>1, up to N concurrent executions are allowed.

Thread safety
-------------
- _pool_lock protects the worker/lock/semaphore dicts during creation.
- _worker_locks[key] protects individual persistent workers from concurrent
  execute() calls.
- Semaphores are accessed via local reference (not dict lookup) to avoid
  a race condition with the reaper thread or shutdown_all().
- Reaper runs every 30s as a daemon thread, shuts down idle workers.
"""

import logging
import threading
import time

from ._worker import Worker

logger = logging.getLogger(__name__)


class WorkerPool:
    """
    Pool of workers with per-step concurrency control.

    Parameters
    ----------
    idle_timeout : float
        Seconds before idle persistent workers are shut down (default: 300).
    connect_timeout : float
        Seconds to wait for a new worker to connect (default: 60).
    """

    def __init__(self, idle_timeout=300.0, connect_timeout=60.0):
        self.idle_timeout = idle_timeout
        self.connect_timeout = connect_timeout

        self._workers = {}          # (env, step_path) -> Worker
        self._worker_locks = {}     # (env, step_path) -> Lock
        self._semaphores = {}       # (env, step_path) -> Semaphore
        self._pool_lock = threading.Lock()
        self._shutdown_event = threading.Event()
        self._reaper = None

    def execute(self, environment, step_path, pipeline_data, params,
                worker_type="subprocess", max_workers=1, timeout=300.0):
        """
        Execute a step. Dispatches to persistent or oneshot worker
        based on worker_type. Blocks if max_workers concurrency is reached.
        """
        key = (environment, str(step_path))

        with self._pool_lock:
            if key not in self._semaphores:
                self._semaphores[key] = threading.Semaphore(max_workers)
                logger.debug("Pool: created semaphore for %s (max_workers=%d)",
                             key, max_workers)
            sem = self._semaphores[key]

        logger.debug("Pool: acquiring semaphore for %s (type=%s)",
                     key, worker_type)
        sem.acquire()
        logger.debug("Pool: semaphore acquired for %s", key)
        try:
            if worker_type == "persistent":
                self._ensure_reaper()
                return self._execute_persistent(
                    key, environment, step_path,
                    pipeline_data, params, timeout,
                )
            else:
                return self._execute_oneshot(
                    environment, step_path,
                    pipeline_data, params, timeout,
                )
        finally:
            sem.release()
            logger.debug("Pool: semaphore released for %s", key)

    def _execute_persistent(self, key, environment, step_path,
                            pipeline_data, params, timeout):
        """Execute via a persistent warm worker."""
        with self._pool_lock:
            if key not in self._workers:
                logger.info("Pool: creating persistent worker for env=%s, "
                            "step=%s", environment, step_path)
                self._workers[key] = Worker(
                    environment=environment,
                    step_path=step_path,
                    idle_timeout=self.idle_timeout,
                    connect_timeout=self.connect_timeout,
                )
                self._worker_locks[key] = threading.Lock()
            else:
                logger.debug("Pool: reusing persistent worker for %s", key)

            worker = self._workers[key]
            lock = self._worker_locks[key]

        with lock:
            return worker.execute(pipeline_data, params, timeout=timeout)

    def _execute_oneshot(self, environment, step_path,
                         pipeline_data, params, timeout):
        """Execute via a oneshot worker (spawn, run, exit)."""
        logger.debug("Pool: creating oneshot worker for env=%s, step=%s",
                     environment, step_path)
        worker = Worker(
            environment=environment,
            step_path=step_path,
            oneshot=True,
            connect_timeout=self.connect_timeout,
        )
        try:
            return worker.execute(pipeline_data, params, timeout=timeout)
        finally:
            worker.shutdown()

    def _ensure_reaper(self):
        """Start the reaper thread on first persistent worker use."""
        with self._pool_lock:
            if self._reaper is None:
                logger.debug("Pool: starting reaper thread "
                             "(idle_timeout=%.0fs)", self.idle_timeout)
                self._reaper = threading.Thread(
                    target=self._reaper_loop, daemon=True,
                )
                self._reaper.start()

    def active_workers(self):
        """Return list of (env, step) keys for alive persistent workers."""
        with self._pool_lock:
            return [k for k, w in self._workers.items() if w.is_alive()]

    def shutdown_all(self):
        """Shut down all persistent workers and stop the reaper."""
        self._shutdown_event.set()
        with self._pool_lock:
            n = len(self._workers)
            if n:
                logger.info("Pool: shutting down %d persistent worker(s)", n)
            for worker in self._workers.values():
                worker.shutdown()
            self._workers.clear()
            self._worker_locks.clear()
            self._semaphores.clear()
        logger.debug("Pool: shutdown complete, all dictionaries cleared")

    def _reaper_loop(self):
        """Background thread that shuts down idle persistent workers."""
        while not self._shutdown_event.wait(timeout=30.0):
            self._reap_idle()

    def _reap_idle(self):
        """Shut down persistent workers that exceed idle_timeout."""
        now = time.monotonic()
        to_shutdown = []

        with self._pool_lock:
            for key, worker in list(self._workers.items()):
                if worker.is_idle(now) and not self._worker_locks[key].locked():
                    to_shutdown.append(self._workers.pop(key))
                    self._worker_locks.pop(key)
                    self._semaphores.pop(key, None)

        if to_shutdown:
            logger.info("Pool: reaping %d idle worker(s)", len(to_shutdown))
        for worker in to_shutdown:
            logger.debug("Pool: reaping %r", worker)
            worker.shutdown()

    def __repr__(self):
        with self._pool_lock:
            alive = sum(1 for w in self._workers.values() if w.is_alive())
            total = len(self._workers)
        return f"WorkerPool(persistent={alive}/{total})"
