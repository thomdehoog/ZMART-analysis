"""
PipelineEngine — Central orchestrator for the v3 pipeline engine.

Creates and manages runs, routes step execution to local or isolated workers,
and provides system-wide observability via status().

Two entry points
----------------
Simple (backwards compatible with v2):

    from engine import run_pipeline
    result = run_pipeline("pipeline.yaml", "label", data)

With runs and scopes:

    engine = PipelineEngine()
    overview = engine.create_run("overview.yaml", priority=10)
    overview.submit("tile_1", data, spatial={"region": "R3"})
    future = overview.scope_complete(spatial={"region": "R3"})
    result = future.result()
    engine.shutdown()

Step routing
------------
For each step, the engine reads METADATA via AST (no code execution) to
determine the target environment and device. Then:

  - local (same env): load module via exec(), call run() in-process
  - isolated (different env): delegate to WorkerPool

Under maximal isolation, ALL steps go through the pool (even same-env ones).
Under minimal, only environment-crossing steps use workers.

The pipeline-level "environment" override in YAML metadata causes local
steps to run in the specified environment instead.
"""

from __future__ import annotations

import logging
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ._loader import get_step_settings, load_function
from ._pool import WorkerPool
from ._run import Run

logger = logging.getLogger(__name__)

PRIORITY_NAMES = {"high": 10, "normal": 0, "low": -10}


def _resolve_priority(priority):
    """Convert string priority to int. Pass through ints unchanged."""
    if isinstance(priority, str):
        return PRIORITY_NAMES.get(priority.lower(), 0)
    return priority


class PipelineEngine:
    """
    Central pipeline orchestrator.

    Parameters
    ----------
    idle_timeout : float
        Seconds before idle persistent workers are shut down (default: 300).
    max_concurrent : int
        Maximum concurrent operations in the thread pool (default: 8).
    execution_timeout : float
        Default timeout for a single step in seconds (default: 300).
    """

    def __init__(self, idle_timeout=300.0, max_concurrent=8,
                 execution_timeout=300.0):
        self.execution_timeout = execution_timeout
        self._pool = WorkerPool(idle_timeout=idle_timeout)
        self._executor = ThreadPoolExecutor(max_workers=max_concurrent)
        self._runs = []
        self._accepting = True
        self._lock = threading.Lock()

        logger.debug("PipelineEngine created: idle_timeout=%.0f, "
                     "max_concurrent=%d, execution_timeout=%.0f",
                     idle_timeout, max_concurrent, execution_timeout)

    def create_run(self, yaml_path, priority=0):
        """
        Create a new pipeline run.

        Parameters
        ----------
        yaml_path : str or Path
            Path to the pipeline YAML file.
        priority : int or str
            Priority level. Higher int = more urgent. 0 = default (FIFO).
            String shortcuts: "high" (10), "normal" (0), "low" (-10).

        Returns
        -------
        Run
        """
        if not self._accepting:
            raise RuntimeError("Engine has been shut down")

        priority = _resolve_priority(priority)
        run = Run(self, yaml_path, priority)
        with self._lock:
            self._runs.append(run)
        return run

    def _execute_step(self, step_config, pipeline_data, functions_dir,
                      priority, isolation, pipeline_env=None):
        """
        Execute a single step, routing to local or worker pool.

        Called by Run during phase execution. Not part of the public API.
        """
        func_path = functions_dir / f"{step_config.name}.py"
        settings = get_step_settings(func_path)

        target_env = settings["environment"]
        device = settings["device"]
        current_env = Path(sys.prefix).name

        # Pipeline-level env override for local steps
        if target_env.lower() == "local" and pipeline_env:
            target_env = pipeline_env

        needs_isolation = (
            isolation == "maximal"
            or (target_env.lower() != "local"
                and target_env.lower() != current_env.lower())
        )

        if needs_isolation:
            return self._pool.execute(
                environment=target_env,
                device=device,
                step_path=str(func_path),
                pipeline_data=pipeline_data,
                params=step_config.params,
                priority=priority,
                timeout=self.execution_timeout,
                isolation=isolation,
            )
        else:
            module = load_function(step_config.name, functions_dir)
            try:
                return module.run(pipeline_data, **step_config.params)
            except SystemExit as e:
                raise RuntimeError(
                    f"Step '{step_config.name}' called sys.exit({e.code}). "
                    f"Use isolation: maximal for untrusted steps."
                ) from e

    def status(self):
        """
        Query the full engine state for dashboards and monitoring.

        Returns
        -------
        dict
            Contains "workers" (list), "gpu_queue_depth" (int),
            and "runs" (list of run status dicts).
        """
        pool_status = self._pool.status
        with self._lock:
            runs_status = [r.status for r in self._runs]
        return {
            **pool_status,
            "runs": runs_status,
        }

    @property
    def pool(self):
        """Access the worker pool for inspection."""
        return self._pool

    def shutdown(self, wait=True):
        """Shut down the engine, thread pool, and all workers."""
        logger.info("Engine shutting down (wait=%s)", wait)
        self._accepting = False
        self._executor.shutdown(wait=wait)
        self._pool.shutdown_all()
        logger.debug("Engine shutdown complete")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.shutdown()
        return False

    def __repr__(self):
        with self._lock:
            n_runs = len(self._runs)
        return f"PipelineEngine(runs={n_runs}, pool={self._pool!r})"


def run_pipeline(yaml_path: str, label: str,
                 input_data: dict | None = None) -> dict:
    """
    Run a pipeline. Creates a temporary engine and cleans up after.

    This is the simplest entry point — backwards compatible with v2.
    Works for pipelines with no scopes (all steps in Phase 0).
    """
    with PipelineEngine() as engine:
        run = engine.create_run(yaml_path)
        future = run.submit(label, input_data)
        return future.result()
