"""
Streaming Engine — Pipeline orchestrator with warm worker pool.

Replaces the batch engine's spawn-run-exit model with persistent workers
that stay alive between invocations. Supports pipeline parallelism: multiple
files can be in-flight at different stages simultaneously.

Usage:
    from streaming import StreamingEngine

    engine = StreamingEngine(idle_timeout=300)

    # Blocking — single file
    result = engine.process_file(
        yaml_path="workflows/rare_event_selection/pipelines/pipeline.yaml",
        label="tile_001",
        input_data={"data_source": "/path/to/tile_001.tif"},
    )

    # Non-blocking — multiple files
    future = engine.submit_file(yaml_path, label, input_data)
    result = future.result()

    engine.shutdown()
"""

import os
import sys
from concurrent.futures import ThreadPoolExecutor, Future
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any

import yaml

# Import shared utilities from the batch engine
ENGINE_DIR = Path(__file__).resolve().parent.parent / "engine"
sys.path.insert(0, str(ENGINE_DIR))
from engine import load_function, get_step_settings
sys.path.pop(0)

from .pool import WorkerPool


class StreamingEngine:
    """
    Pipeline engine with persistent warm workers.

    Parameters
    ----------
    idle_timeout : float
        Seconds before idle workers are shut down (default: 300).
    max_concurrent : int
        Maximum number of files processed simultaneously (default: 8).
    connect_timeout : float
        Seconds to wait for a new worker to connect (default: 60).
    execution_timeout : float
        Default timeout for a single step execution (default: 300).
    """

    def __init__(self, idle_timeout=300.0, max_concurrent=8,
                 connect_timeout=60.0, execution_timeout=300.0):
        self.idle_timeout = idle_timeout
        self.max_concurrent = max_concurrent
        self.execution_timeout = execution_timeout

        self._pool = WorkerPool(
            idle_timeout=idle_timeout,
            connect_timeout=connect_timeout,
        )
        self._executor = ThreadPoolExecutor(max_workers=max_concurrent)
        self._accepting = True

    def process_file(self, yaml_path, label, input_data=None):
        """
        Process a single file through a pipeline. Blocking call.

        Parameters
        ----------
        yaml_path : str
            Path to the YAML pipeline configuration file.
        label : str
            Human-readable label for this run.
        input_data : dict, optional
            Input data for the pipeline.

        Returns
        -------
        dict
            The final pipeline_data dictionary.
        """
        yaml_path = Path(yaml_path)

        # Load YAML
        with open(yaml_path, "r") as f:
            config = yaml.safe_load(f)

        yaml_metadata = config.get("metadata", {})
        verbose = yaml_metadata.get("verbose", 0)

        # Determine functions directory
        functions_dir_str = yaml_metadata.get("functions_dir", "../steps")
        functions_dir = Path(os.path.abspath(yaml_path.parent / functions_dir_str))

        # Find the workflow key
        workflow_name = None
        for key in config:
            if key != "metadata":
                workflow_name = key
                break

        if not workflow_name:
            raise ValueError("No workflow found in YAML (need a key other than 'metadata')")

        steps_config = config[workflow_name]
        step_names = [list(s.keys())[0] for s in steps_config]

        # Pipeline-level environment (default for steps declaring "local")
        pipeline_env = yaml_metadata.get("environment")

        def engine_log(msg):
            if verbose in (1, 3):
                print(msg)

        engine_log(f"[streaming] Pipeline: {yaml_path}")
        engine_log(f"[streaming] Workflow: {workflow_name}")
        engine_log(f"[streaming] Label: {label}")
        engine_log(f"[streaming] Steps: {step_names}")

        # Initialize pipeline_data (same structure as batch engine)
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
            "input": input_data or {},
        }

        # Current environment for comparison
        current_env = os.path.basename(sys.prefix)

        # Execute each step
        for step_idx, step_config in enumerate(steps_config, start=1):
            func_name = list(step_config.keys())[0]
            params = step_config[func_name] or {}

            engine_log(f"\n[streaming] Step {step_idx}/{len(steps_config)}: {func_name}")

            # Load module locally to read METADATA
            module = load_function(func_name, functions_dir)
            settings = get_step_settings(module)

            target_env = settings["environment"]

            # Resolve effective environment:
            # If step says "local" but pipeline declares an environment,
            # the step should run in the pipeline's environment.
            if target_env.lower() == "local" and pipeline_env:
                target_env = pipeline_env

            # Determine if we need a worker (subprocess)
            needs_worker = (
                target_env.lower() != "local"
                and target_env.lower() != current_env.lower()
            )

            engine_log(f"[streaming]   Environment: {target_env} "
                        f"({'worker' if needs_worker else 'local'})")

            if needs_worker:
                func_path = functions_dir / f"{func_name}.py"
                pipeline_data = self._pool.execute(
                    environment=target_env,
                    step_path=str(func_path),
                    pipeline_data=pipeline_data,
                    params=params,
                    timeout=self.execution_timeout,
                )
            else:
                pipeline_data = module.run(pipeline_data, **params)

            engine_log(f"[streaming]   Completed: {func_name}")

        engine_log(f"\n[streaming] Pipeline complete")
        return pipeline_data

    def submit_file(self, yaml_path, label, input_data=None, callback=None):
        """
        Submit a file for asynchronous processing. Non-blocking.

        Parameters
        ----------
        yaml_path : str
            Path to the YAML pipeline configuration file.
        label : str
            Human-readable label for this run.
        input_data : dict, optional
            Input data for the pipeline.
        callback : callable, optional
            Called with the Future when processing completes.

        Returns
        -------
        concurrent.futures.Future
            Future containing the pipeline result.

        Raises
        ------
        RuntimeError
            If the engine has been shut down.
        """
        if not self._accepting:
            raise RuntimeError("Engine has been shut down")

        future = self._executor.submit(
            self.process_file, yaml_path, label, input_data
        )

        if callback is not None:
            future.add_done_callback(callback)

        return future

    @property
    def pool(self):
        """Access the worker pool for inspection."""
        return self._pool

    def shutdown(self, wait=True):
        """
        Shut down the engine, thread pool, and all workers.

        Parameters
        ----------
        wait : bool
            If True, wait for in-flight files to complete before shutdown.
        """
        self._accepting = False
        self._executor.shutdown(wait=wait)
        self._pool.shutdown_all()

    def __repr__(self):
        return (f"StreamingEngine(pool={self._pool!r}, "
                f"max_concurrent={self.max_concurrent})")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.shutdown()
        return False
