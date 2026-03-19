"""
WorkerPool — Maps (environment, step_path) to warm Worker instances.

Handles lazy spawning, worker sharing across pipelines, and automatic
shutdown of idle workers via a background reaper thread.
"""

import threading
import time

from .worker import Worker


class WorkerPool:
    """
    Pool of persistent worker processes, keyed by (environment, step_path).

    Workers are created lazily on first use and shared across pipelines.
    A background reaper thread periodically shuts down idle workers.

    Parameters
    ----------
    idle_timeout : float
        Seconds of inactivity before a worker is shut down (default: 300).
    reaper_interval : float
        How often the reaper thread checks for idle workers (default: 30).
    connect_timeout : float
        Seconds to wait for a new worker to connect (default: 60).
    """

    def __init__(self, idle_timeout=300.0, reaper_interval=30.0,
                 connect_timeout=60.0):
        self.idle_timeout = idle_timeout
        self.reaper_interval = reaper_interval
        self.connect_timeout = connect_timeout

        self._workers = {}          # (env, step_path) -> Worker
        self._worker_locks = {}     # (env, step_path) -> Lock (serializes calls)
        self._pool_lock = threading.Lock()  # protects dict mutations
        self._shutdown_event = threading.Event()

        # Start reaper thread
        self._reaper = threading.Thread(target=self._reaper_loop, daemon=True)
        self._reaper.start()

    def get_worker(self, environment, step_path):
        """
        Get or create a Worker for the given (environment, step_path).

        Thread-safe. Returns the worker and its execution lock.
        """
        key = (environment, str(step_path))

        with self._pool_lock:
            if key not in self._workers:
                self._workers[key] = Worker(
                    environment=environment,
                    step_path=step_path,
                    idle_timeout=self.idle_timeout,
                    connect_timeout=self.connect_timeout,
                )
                self._worker_locks[key] = threading.Lock()

            return self._workers[key], self._worker_locks[key]

    def execute(self, environment, step_path, pipeline_data, params,
                timeout=300.0):
        """
        Execute a step on the appropriate worker. Thread-safe.

        Acquires the per-worker lock so concurrent calls to the same
        worker are serialized (a worker handles one request at a time).

        Parameters
        ----------
        environment : str
            Conda environment name.
        step_path : str
            Absolute path to the step file.
        pipeline_data : dict
            Current pipeline data.
        params : dict
            Step parameters from YAML.
        timeout : float
            Execution timeout in seconds.

        Returns
        -------
        dict
            Updated pipeline_data.
        """
        worker, lock = self.get_worker(environment, step_path)

        with lock:
            return worker.execute(pipeline_data, params, timeout=timeout)

    def worker_count(self):
        """Return the number of workers currently in the pool."""
        with self._pool_lock:
            return len(self._workers)

    def active_workers(self):
        """Return list of (env, step) keys for alive workers."""
        with self._pool_lock:
            return [
                key for key, w in self._workers.items() if w.is_alive()
            ]

    def shutdown_all(self):
        """Shut down all workers and stop the reaper thread."""
        self._shutdown_event.set()

        with self._pool_lock:
            for worker in self._workers.values():
                worker.shutdown()
            self._workers.clear()
            self._worker_locks.clear()

    def _reaper_loop(self):
        """Background thread that shuts down idle workers."""
        while not self._shutdown_event.wait(timeout=self.reaper_interval):
            self._reap_idle()

    def _reap_idle(self):
        """Check all workers and shut down any that exceed idle_timeout."""
        now = time.monotonic()
        to_remove = []

        with self._pool_lock:
            for key, worker in self._workers.items():
                if worker.is_idle(now) and not self._worker_locks[key].locked():
                    to_remove.append(key)

        for key in to_remove:
            with self._pool_lock:
                worker = self._workers.pop(key, None)
                self._worker_locks.pop(key, None)

            if worker is not None:
                worker.shutdown()

    def __repr__(self):
        with self._pool_lock:
            alive = sum(1 for w in self._workers.values() if w.is_alive())
            total = len(self._workers)
        return f"WorkerPool(workers={alive}/{total}, timeout={self.idle_timeout}s)"

    def __del__(self):
        self.shutdown_all()
