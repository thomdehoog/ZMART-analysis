"""
PipelineEngine — Unified orchestrator for sequential Python pipelines.

Reads YAML pipeline configs, iterates over steps, and delegates execution
based on each step's METADATA settings (environment, worker type,
concurrency limits).
"""

import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import yaml

from ._loader import load_function, get_step_settings
from ._pool import WorkerPool


class PipelineEngine:
    """
    Pipeline engine with per-step worker management.

    Each step declares its execution preferences in METADATA:
      - environment: conda env name or "local"
      - worker: "persistent" (warm) or "subprocess" (spawn-run-exit)
      - max_workers: concurrency limit for multi-file processing

    Parameters
    ----------
    idle_timeout : float
        Seconds before idle persistent workers are shut down (default: 300).
    max_concurrent : int
        Maximum pipelines processed simultaneously via submit() (default: 8).
    execution_timeout : float
        Default timeout for a single step in seconds (default: 300).
    """

    def __init__(self, idle_timeout=300.0, max_concurrent=8,
                 execution_timeout=300.0):
        self.execution_timeout = execution_timeout
        self._pool = WorkerPool(idle_timeout=idle_timeout)
        self._executor = ThreadPoolExecutor(max_workers=max_concurrent)
        self._accepting = True

    def run_pipeline(self, yaml_path, label, input_data=None):
        """
        Run a complete pipeline from a YAML configuration file. Blocking.

        Parameters
        ----------
        yaml_path : str
            Path to the YAML pipeline file.
        label : str
            Human-readable label for this run.
        input_data : dict, optional
            Input data for the pipeline.

        Returns
        -------
        dict
            The final pipeline_data dictionary.
        """
        if not self._accepting:
            raise RuntimeError("Engine has been shut down")

        yaml_path = Path(yaml_path)

        with open(yaml_path, "r") as f:
            config = yaml.safe_load(f)

        yaml_metadata = config.get("metadata", {})
        verbose = yaml_metadata.get("verbose", 0)

        functions_dir_str = yaml_metadata.get("functions_dir", "../steps")
        functions_dir = (yaml_path.parent / functions_dir_str).resolve()

        # Find workflow key (first key that isn't 'metadata')
        workflow_name = None
        for key in config:
            if key != "metadata":
                workflow_name = key
                break

        if not workflow_name:
            raise ValueError(
                "No workflow found in YAML (need a key other than 'metadata')"
            )

        steps_config = config[workflow_name] or []
        if not steps_config:
            raise ValueError(
                f"Workflow '{workflow_name}' has no steps"
            )
        step_names = [list(s.keys())[0] for s in steps_config]

        pipeline_env = yaml_metadata.get("environment")
        current_env = Path(sys.prefix).name

        def engine_log(msg):
            if verbose in (1, 3):
                print(msg)

        engine_log(f"[engine] Pipeline: {yaml_path}")
        engine_log(f"[engine] Workflow: {workflow_name}")
        engine_log(f"[engine] Label: {label}")
        engine_log(f"[engine] Steps: {step_names}")

        pipeline_data = {
            "metadata": {
                "datetime": datetime.now().strftime("%Y%m%d-%H%M%S"),
                "label": label,
                "workflow_name": workflow_name,
                "yaml_filename": yaml_path.name,
                "steps": step_names,
                "verbose": verbose,
                **{k: v for k, v in yaml_metadata.items()
                   if k not in ("verbose", "functions_dir", "environment")},
            },
            "input": input_data if input_data is not None else {},
        }

        for step_idx, step_config in enumerate(steps_config, start=1):
            func_name = list(step_config.keys())[0]
            params = step_config[func_name] or {}

            engine_log(
                f"\n[engine] Step {step_idx}/{len(steps_config)}: {func_name}"
            )

            func_path = functions_dir / f"{func_name}.py"
            settings = get_step_settings(func_path)

            target_env = settings["environment"]
            worker_type = settings["worker"]
            max_workers = settings["max_workers"]

            # If step says "local" but pipeline declares an environment,
            # run in the pipeline's environment.
            if target_env.lower() == "local" and pipeline_env:
                target_env = pipeline_env

            needs_isolation = (
                target_env.lower() != "local"
                and target_env.lower() != current_env.lower()
            )

            mode = "local" if not needs_isolation else worker_type
            engine_log(f"[engine]   Environment: {target_env} ({mode})")

            if needs_isolation:
                pipeline_data = self._pool.execute(
                    environment=target_env,
                    step_path=str(func_path),
                    pipeline_data=pipeline_data,
                    params=params,
                    worker_type=worker_type,
                    max_workers=max_workers,
                    timeout=self.execution_timeout,
                )
            else:
                module = load_function(func_name, functions_dir)
                pipeline_data = module.run(pipeline_data, **params)

            if not isinstance(pipeline_data, dict):
                raise TypeError(
                    f"Step '{func_name}' returned {type(pipeline_data).__name__}, "
                    f"expected dict"
                )

            engine_log(f"[engine]   Completed: {func_name}")

        engine_log(f"\n[engine] Pipeline complete")
        return pipeline_data

    def submit(self, yaml_path, label, input_data=None, callback=None):
        """
        Submit a pipeline for asynchronous processing. Non-blocking.

        Returns
        -------
        concurrent.futures.Future
        """
        if not self._accepting:
            raise RuntimeError("Engine has been shut down")

        future = self._executor.submit(
            self.run_pipeline, yaml_path, label, input_data,
        )
        if callback:
            future.add_done_callback(callback)
        return future

    @property
    def pool(self):
        """Access the worker pool for inspection."""
        return self._pool

    def shutdown(self, wait=True):
        """Shut down the engine, thread pool, and all workers."""
        self._accepting = False
        self._executor.shutdown(wait=wait)
        self._pool.shutdown_all()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.shutdown()
        return False

    def __repr__(self):
        return f"PipelineEngine(pool={self._pool!r})"


def run_pipeline(yaml_path: str, label: str,
                 input_data: dict | None = None) -> dict:
    """
    Run a pipeline. Creates a temporary engine and cleans up after.

    This is the simplest entry point — same API regardless of whether
    steps use persistent workers or subprocesses.
    """
    with PipelineEngine() as engine:
        return engine.run_pipeline(yaml_path, label, input_data)
