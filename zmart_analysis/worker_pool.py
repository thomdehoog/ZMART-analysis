"""
Worker pool -- the workers of every environment, and who may use them.

The engine asks the pool to run a chain of steps (``WorkerPool.execute``).
The pool keeps one ``EnvPool`` per conda environment, with its idle and busy
workers, and an ordered gate per step and width for ``max_workers``
(job_queue.py): a chain takes the gate of each of its steps, in one fixed
order, then a worker of its environment.

Thread safety: the pool guards its dictionaries with locks, and each
EnvPool guards its idle and busy lists.
"""

import logging
import threading
import weakref
from pathlib import Path

from .job_queue import OrderedGate
from .workers import Worker, WorkerCrashedError, label

logger = logging.getLogger(__name__)


class EnvPool:
    """Worker pool for one conda environment.

    Tracks idle and busy workers, and for every busy one the owner it runs
    for. Creates new workers on demand; they stay until the pool is shut
    down or their owner is killed.
    """

    def __init__(self, environment, connect_timeout):
        self.environment = environment
        self.connect_timeout = connect_timeout
        self.idle = []
        self.busy = []
        self.owners = {}
        self.lock = threading.Lock()
        self.closed = False

    def acquire(self, owner=None):
        """Get an idle worker or create a new one. Marks it busy for *owner*."""
        with self.lock:
            if self.closed:
                raise RuntimeError("WorkerPool has been shut down")
            worker = None
            while self.idle and worker is None:
                candidate = self.idle.pop()
                if candidate.is_alive():
                    worker = candidate
                else:
                    # Dead worker, discard silently
                    candidate.shutdown()
            if worker is None:
                worker = Worker(environment=self.environment, connect_timeout=self.connect_timeout)
            self.busy.append(worker)
            self.owners[worker] = owner
            return worker

    def release(self, worker):
        """Return a worker to the idle pool."""
        with self.lock:
            try:
                self.busy.remove(worker)
            except ValueError:
                pass
            self.owners.pop(worker, None)
            if worker.is_alive() and not self.closed:
                self.idle.append(worker)

    def kill_owner(self, owner):
        """Kill every busy worker running for *owner*, at once.

        The caller blocked on such a worker is released with
        WorkerCrashedError. The other workers of the pool are left alone.
        """
        with self.lock:
            doomed = [w for w in self.busy if self.owners.get(w) is owner]
            for worker in doomed:
                self.busy.remove(worker)
                self.owners.pop(worker, None)
        for worker in doomed:
            worker.shutdown(now=True)

    def shutdown_all(self, now=False):
        """Shut down all workers in this pool; with ``now``, at once."""
        with self.lock:
            self.closed = True
            all_workers = self.idle + self.busy
            self.idle.clear()
            self.busy.clear()
            self.owners.clear()

        for worker in all_workers:
            worker.shutdown(now=now)

    @property
    def status(self):
        """Current pool state."""
        workers = []
        with self.lock:
            for worker in self.busy + self.idle:
                if worker.is_alive():
                    workers.append(worker.status)
        return workers


class WorkerPool:
    """
    Pool of per-environment worker pools with per-step concurrency.

    Parameters
    ----------
    connect_timeout : float
        Seconds to wait for a new worker to connect (default: 60).
    """

    def __init__(self, connect_timeout=60.0):
        self.connect_timeout = connect_timeout

        self.env_pools = {}
        self.pool_lock = threading.Lock()

        #: One gate per step file and width, for ``max_workers``.
        self.gates = {}
        self.gates_lock = threading.Lock()

        #: Owners whose steps were killed. Their later steps are refused.
        self.killed = weakref.WeakSet()
        self.closed = False

    def execute(
        self,
        environment,
        steps,
        pipeline_data,
        timeout=300.0,
        owner=None,
        files=None,
        started=None,
        order=None,
        report=None,
    ):
        """
        Run a chain of steps in a worker of *environment*.

        Blocks until each step of the chain may run (its ``max_workers``)
        and a worker is free, then until the chain is done. Callers waiting
        for the same step are let in by their order key (job_queue.py).

        Parameters
        ----------
        environment : str or None
            Conda environment. None = orchestrator's environment.
        steps : list of (str, dict, int)
            The chain: each step's file, its parameters from the YAML, and
            how many of it may run at once.
        pipeline_data : dict
            The data for the first step.
        timeout : float or None
            Seconds each step may take.
        owner : object, optional
            Who the steps run for; :meth:`kill_owner` stops them.
        files : dict, optional
            How the data goes through folders (see worker_script.py).
        started : callable, optional
            Called once a worker has taken the chain, just before it runs.
            Until then the chain only waits, for its turn or for a worker.
        order : tuple, optional
            The job's order key; without one, the call waits behind every
            keyed one.
        report : callable, optional
            Called with each step's start and record (see Worker.execute).
        """
        # Each step's own gate, taken in one fixed order so that two chains
        # that share steps can never hold each other up.
        gates = [
            self.gate_for(path, width) for path, width in sorted({(p, w) for p, _, w in steps})
        ]
        taken = []
        try:
            for gate in gates:
                gate.acquire(order)
                taken.append(gate)
            while True:
                pool = self.env_pool_for(environment)
                try:
                    worker = pool.acquire(owner)
                    break
                except RuntimeError:
                    # The environment was taken away between the two calls;
                    # the next turn finds a fresh pool for it.
                    if self.closed:
                        raise
            try:
                # Checked after the worker is marked busy for the owner, and
                # kill_owner marks the owner before it looks for busy
                # workers: a step racing a kill is either refused here or
                # killed there, never started unseen.
                with self.pool_lock:
                    refused = owner is not None and owner in self.killed
                if refused:
                    raise WorkerCrashedError(
                        "Step not started: its pipeline was cancelled", Path(steps[0][0]).stem
                    )
                if started is not None:
                    started()
                return worker.execute(
                    [(path, params) for path, params, _ in steps],
                    pipeline_data,
                    timeout=timeout,
                    files=files,
                    report=report,
                )
            finally:
                pool.release(worker)
        finally:
            for gate in reversed(taken):
                gate.release()

    def env_pool_for(self, environment):
        """Get or create the pool for an environment."""
        with self.pool_lock:
            if self.closed:
                raise RuntimeError("WorkerPool has been shut down")
            if environment not in self.env_pools:
                env_label = label(environment)
                logger.info("Creating env pool for %s", env_label)
                self.env_pools[environment] = EnvPool(environment, self.connect_timeout)
            return self.env_pools[environment]

    def gate_for(self, step_path, max_workers):
        """The gate of a step at this width, for its ``max_workers``.

        Keyed by the width as well as the file: two pipelines that share a
        step file and ask for different widths each get their own, rather
        than whichever width was registered first.
        """
        key = (step_path, int(max_workers))
        with self.gates_lock:
            if key not in self.gates:
                self.gates[key] = OrderedGate(max_workers)
            return self.gates[key]

    # -- Ending part of the pool ---------------------------------------

    def take_environment(self, environment):
        """Take an environment's workers out of the pool and return them.

        Returns the environment's pool, or None when it has none. A later
        step in that environment gets a fresh pool. The caller shuts the
        returned pool down, outside any lock it holds.
        """
        with self.pool_lock:
            return self.env_pools.pop(environment, None)

    def kill_owner(self, owner):
        """Kill every step running for *owner*, in every environment, and
        refuse every step it asks for from now on."""
        with self.pool_lock:
            self.killed.add(owner)
            pools = list(self.env_pools.values())
        for pool in pools:
            pool.kill_owner(owner)

    # -- Status & shutdown ---------------------------------------------

    @property
    def status(self):
        """Current pool state for observability."""
        workers = []
        with self.pool_lock:
            for pool in self.env_pools.values():
                workers.extend(pool.status)
        return {"workers": workers}

    def shutdown_all(self, now=False):
        """Shut down all workers and stop background threads.

        With ``now`` every worker is killed at once, busy or not, so a step
        in flight stops too.
        """
        with self.pool_lock:
            self.closed = True
            pools = list(self.env_pools.values())
            n = len(pools)

        if n:
            logger.info("Shutting down %d env pool(s) (now=%s)", n, now)
        for pool in pools:
            pool.shutdown_all(now=now)

        logger.debug("WorkerPool shutdown complete")

    def __repr__(self):
        with self.pool_lock:
            n_envs = len(self.env_pools)
        return f"WorkerPool(envs={n_envs})"
