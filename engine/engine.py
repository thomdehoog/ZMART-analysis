"""
Pipeline Engine

Orchestration engine that executes a sequence of Python functions defined in YAML.
Supports environment switching via Conda and flexible data transfer between steps.

Usage:
    from engine import run_pipeline
    
    result = run_pipeline(
        yaml_path="workflows/my-workflow/pipelines/pipeline.yaml",
        label="experiment_001",
        input_data={"data_path": "/path/to/data"}
    )

Directory Structure:
    analysis_workflows/
    ├── engine/
    │   └── engine.py          # THIS FILE
    └── workflows/
        └── {workflow}/
            ├── pipelines/
            │   └── {name}_pipeline.yaml
            └── steps/
                └── {step}.py
"""

import os
import sys
import yaml
import importlib.util
import subprocess
from conda_utils import get_conda_info, get_conda_exe
import json
import pickle
import tempfile
import types
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any


def load_function(func_name: str, functions_dir: Path):
    """
    Load a function module from the functions directory.

    Uses exec-based loading instead of importlib.util.spec_from_file_location
    to avoid Windows DLL search path side effects that can break packages
    like PyTorch when step files are on network drives.

    Parameters
    ----------
    func_name : str
        Name of the function (without .py extension)
    functions_dir : Path
        Directory containing function files

    Returns
    -------
    module
        The loaded Python module

    Raises
    ------
    FileNotFoundError
        If the function file doesn't exist
    """
    func_path = functions_dir / f"{func_name}.py"

    if not func_path.exists():
        raise FileNotFoundError(f"Function file not found: {func_path}")

    namespace = {"__name__": func_name, "__file__": str(func_path)}
    with open(func_path) as f:
        exec(compile(f.read(), str(func_path), "exec"), namespace)

    module = types.ModuleType(func_name)
    module.__dict__.update(namespace)

    return module


def get_step_settings(module) -> Dict[str, Any]:
    """
    Get execution settings from a function's METADATA.
    
    Parameters
    ----------
    module : module
        The loaded function module
        
    Returns
    -------
    dict
        Settings including:
        - environment: "local" or conda environment name
        - data_transfer: "file_paths" or "pickle"
    """
    metadata = getattr(module, 'METADATA', {})
    
    return {
        "environment": metadata.get('environment', 'local'),
        "data_transfer": metadata.get('data_transfer', 'file_paths'),
    }


def run_in_subprocess(func_path: str, pipeline_data: dict, params: dict, 
                      environment: str = None, data_transfer: str = "file_paths") -> dict:
    """
    Run a function in a subprocess, optionally in a different Conda environment.
    
    Parameters
    ----------
    func_path : str
        Path to the function file
    pipeline_data : dict
        Current pipeline data dictionary
    params : dict
        Parameters to pass to the function
    environment : str, optional
        Conda environment name. If None, uses current Python.
    data_transfer : str
        "file_paths" (default) - uses JSON, expects paths not large data
        "pickle" - uses pickle for complex objects
        
    Returns
    -------
    dict
        Updated pipeline_data from the subprocess
    """
    
    if data_transfer == "pickle":
        return _run_subprocess_pickle(func_path, pipeline_data, params, environment)
    else:
        return _run_subprocess_json(func_path, pipeline_data, params, environment)


def _run_subprocess_json(func_path: str, pipeline_data: dict, params: dict,
                         environment: str = None) -> dict:
    """Run subprocess using JSON serialization (for file_paths mode)."""
    
    # Create a temporary script to run the function
    script = f'''
import sys
import json
import importlib.util

# Load the function
spec = importlib.util.spec_from_file_location("func", {repr(str(func_path))})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

# Read input
pipeline_data = json.loads({repr(json.dumps(pipeline_data))})
params = json.loads({repr(json.dumps(params))})

# Run
result = module.run(pipeline_data, **params)

# Output
print("__RESULT_START__")
print(json.dumps(result))
print("__RESULT_END__")
'''
    
    return _execute_script(script, environment)


def _run_subprocess_pickle(func_path: str, pipeline_data: dict, params: dict,
                           environment: str = None) -> dict:
    """Run subprocess using pickle serialization (for complex objects)."""
    
    # Write pipeline_data to temp pickle file
    with tempfile.NamedTemporaryFile(mode='wb', suffix='.pkl', delete=False) as f:
        pickle.dump({'pipeline_data': pipeline_data, 'params': params}, f)
        data_file = f.name
    
    # Write result to separate temp file
    result_file = tempfile.mktemp(suffix='.pkl')
    
    script = f'''
import sys
import pickle
import importlib.util

# Load the function
spec = importlib.util.spec_from_file_location("func", {repr(str(func_path))})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

# Read input from pickle
with open({repr(data_file)}, 'rb') as f:
    data = pickle.load(f)

pipeline_data = data['pipeline_data']
params = data['params']

# Run
result = module.run(pipeline_data, **params)

# Write result to pickle
with open({repr(result_file)}, 'wb') as f:
    pickle.dump(result, f)

print("__PICKLE_DONE__")
'''
    
    try:
        _execute_script(script, environment, expect_json=False)
        
        # Read result from pickle file
        with open(result_file, 'rb') as f:
            result = pickle.load(f)
        
        return result
        
    finally:
        # Cleanup temp files
        if os.path.exists(data_file):
            os.unlink(data_file)
        if os.path.exists(result_file):
            os.unlink(result_file)


def _execute_script(script: str, environment: str = None, 
                    expect_json: bool = True, timeout: int = 300) -> dict:
    """
    Execute a Python script in a subprocess.
    
    Parameters
    ----------
    script : str
        Python script content
    environment : str, optional
        Conda environment name
    expect_json : bool
        If True, parse JSON result from stdout markers
    timeout : int
        Subprocess timeout in seconds
        
    Returns
    -------
    dict
        Parsed result (if expect_json) or empty dict
    """
    # Write script to temp file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write(script)
        script_path = f.name
    
    try:
        # Build command
        if environment and environment.lower() != 'local':
            conda_exe = get_conda_exe(get_conda_info())
            cmd = [conda_exe, "run", "-n", environment, "python", script_path]
        else:
            cmd = [sys.executable, script_path]
        
        # Set UTF-8 encoding
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        
        # Execute
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env
        )
        
        if result.returncode != 0:
            raise RuntimeError(f"Subprocess failed: {result.stderr}")
        
        if not expect_json:
            return {}
        
        # Parse JSON result
        output = result.stdout
        start_marker = "__RESULT_START__"
        end_marker = "__RESULT_END__"
        
        start_idx = output.find(start_marker)
        end_idx = output.find(end_marker)
        
        if start_idx == -1 or end_idx == -1:
            raise RuntimeError(f"Could not find result markers in output: {output}")
        
        json_str = output[start_idx + len(start_marker):end_idx].strip()
        return json.loads(json_str)
        
    finally:
        os.unlink(script_path)


def run_pipeline(yaml_path: str, label: str, input_data: Optional[Dict] = None) -> dict:
    """
    Run a complete workflow from a YAML configuration file.
    
    Parameters
    ----------
    yaml_path : str
        Path to the YAML configuration file.
    label : str
        Human-readable label for this run. Used in output folder names.
        Example: "experiment_001", "test_run_20260127"
    input_data : dict, optional
        Input data to process. Should be a dictionary with informative keys.
        Example: {"data_path": "/path/to/data", "file_list": [...]}
        Can be empty dict {} if first step generates/selects data.
    
    Returns
    -------
    dict
        The final pipeline_data dictionary containing:
        - "metadata": Run information (datetime, label, workflow_name, etc.)
        - "input": Original input data
        - "{step_name}": Output from each step
    
    Raises
    ------
    FileNotFoundError
        If YAML file or step files don't exist.
    ValueError
        If YAML is malformed or missing required fields.
    RuntimeError
        If a step fails during execution.
    
    Examples
    --------
    >>> result = run_pipeline(
    ...     yaml_path="workflows/analysis/pipelines/analysis_pipeline.yaml",
    ...     label="experiment_001",
    ...     input_data={"data_path": "/path/to/images"}
    ... )
    >>> print(result["segmentation"]["output_path"])
    """
    yaml_path = Path(yaml_path)
    
    # Load YAML
    with open(yaml_path, 'r') as f:
        config = yaml.safe_load(f)
    
    yaml_metadata = config.get('metadata', {})
    verbose = yaml_metadata.get('verbose', 0)
    
    # Determine functions directory
    # Default: sibling 'steps' folder relative to pipelines folder
    functions_dir_str = yaml_metadata.get('functions_dir', '../steps')
    functions_dir = Path(os.path.abspath(yaml_path.parent / functions_dir_str))
    
    # Find the workflow key (any key that isn't 'metadata')
    workflow_name = None
    for key in config:
        if key != 'metadata':
            workflow_name = key
            break
    
    if not workflow_name:
        raise ValueError("No workflow found in YAML (need a key other than 'metadata')")
    
    steps_config = config[workflow_name]
    
    # Get step names for metadata
    step_names = [list(s.keys())[0] for s in steps_config]
    
    # Engine logging (verbose levels 1 and 3)
    def engine_log(msg):
        if verbose in (1, 3):
            print(msg)
    
    engine_log(f"[engine] Pipeline: {yaml_path}")
    engine_log(f"[engine] Workflow: {workflow_name}")
    engine_log(f"[engine] Functions dir: {functions_dir}")
    engine_log(f"[engine] Label: {label}")
    engine_log(f"[engine] Steps: {step_names}")
    
    # Check for pipeline-level environment
    pipeline_env = yaml_metadata.get('environment')
    current_env = os.path.basename(sys.prefix)
    
    # Handle pipeline-level environment switching
    if pipeline_env and pipeline_env.lower() not in ('local', current_env.lower()):
        engine_log(f"[engine] Pipeline requires environment '{pipeline_env}', "
                   f"current is '{current_env}'")
        engine_log(f"[engine] Re-running entire pipeline in '{pipeline_env}'...")
        
        return _run_pipeline_in_environment(
            yaml_path, label, input_data, pipeline_env
        )
    
    # Initialize pipeline_data with new structure
    pipeline_data = {
        "metadata": {
            "datetime": datetime.now().strftime("%Y%m%d-%H%M%S"),
            "label": label,
            "workflow_name": workflow_name,
            "yaml_filename": yaml_path.name,
            "steps": step_names,
            "verbose": verbose,
            # Include other YAML metadata fields
            **{k: v for k, v in yaml_metadata.items() 
               if k not in ('verbose', 'functions_dir', 'environment')}
        },
        "input": input_data or {}
    }
    
    # Execute each step
    for step_idx, step_config in enumerate(steps_config, start=1):
        # Each step is a dict with one key (the function name)
        func_name = list(step_config.keys())[0]
        params = step_config[func_name] or {}
        
        engine_log(f"\n[engine] Step {step_idx}/{len(steps_config)}: {func_name}")
        
        # Load function to check its settings
        module = load_function(func_name, functions_dir)
        settings = get_step_settings(module)
        
        target_env = settings['environment']
        data_transfer = settings['data_transfer']
        
        engine_log(f"[engine]   Environment: {target_env}")
        if target_env.lower() != 'local':
            engine_log(f"[engine]   Data transfer: {data_transfer}")
        
        # Determine if we need subprocess
        needs_subprocess = (
            target_env.lower() != 'local' and 
            target_env.lower() != current_env.lower()
        )
        
        # Execute step
        if needs_subprocess:
            func_path = functions_dir / f"{func_name}.py"
            pipeline_data = run_in_subprocess(
                str(func_path), pipeline_data, params, 
                target_env, data_transfer
            )
        else:
            # Run in current process
            pipeline_data = module.run(pipeline_data, **params)
        
        engine_log(f"[engine]   Completed: {func_name}")
    
    engine_log(f"\n[engine] Pipeline complete")
    
    return pipeline_data


def _run_pipeline_in_environment(yaml_path: Path, label: str, 
                                  input_data: Optional[Dict], 
                                  environment: str) -> dict:
    """
    Re-run the entire pipeline in a different Conda environment.
    
    This is called when the YAML metadata specifies a pipeline-level environment
    that differs from the current environment.
    """
    # Serialize input_data for the subprocess
    input_json = json.dumps(input_data) if input_data else 'None'
    
    engine_dir = os.path.abspath(Path(__file__).parent)

    script = f'''
import sys
import json
from pathlib import Path

# Add engine directory to path
sys.path.insert(0, {repr(engine_dir)})

from engine import run_pipeline

input_data = json.loads({repr(input_json)}) if {repr(input_json)} != 'None' else None

result = run_pipeline(
    yaml_path={repr(str(yaml_path))},
    label={repr(label)},
    input_data=input_data
)

print("__RESULT_START__")
print(json.dumps(result))
print("__RESULT_END__")
'''
    
    return _execute_script(script, environment, timeout=600)


if __name__ == "__main__":
    # Simple CLI interface
    import argparse
    
    parser = argparse.ArgumentParser(description="Run a pipeline from YAML")
    parser.add_argument("yaml_path", help="Path to pipeline YAML file")
    parser.add_argument("--label", default="test", help="Run label")
    parser.add_argument("--input", type=json.loads, default={}, 
                        help="Input data as JSON string")
    
    args = parser.parse_args()
    
    result = run_pipeline(
        yaml_path=args.yaml_path,
        label=args.label,
        input_data=args.input
    )
    
    print("\n" + "=" * 60)
    print("Pipeline Result:")
    print("=" * 60)
    print(json.dumps(result, indent=2, default=str))
