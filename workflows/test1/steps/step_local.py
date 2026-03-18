"""
Test Function - Local Execution

A simple function that runs locally (no isolation).
Used to test that local execution works correctly.

IMPORTANT: All imports must be inside the run() function
to support environment switching.
"""

METADATA = {
    "description": "Test function - runs locally",
    "version": "1.0",
    "environment": "local"  # Default - runs in current process
}


def run(pipeline_data: dict, **params) -> dict:
    """Execute locally in the main process."""
    import sys
    import os
    
    env_name = os.path.basename(sys.prefix)
    verbose = pipeline_data["metadata"].get("verbose", 0)
    
    # Step output (verbose levels 2 and 3)
    if verbose >= 2:
        print()
        print("=" * 50)
        print("  STEP: step_local")
        print("=" * 50)
        print(f"    Process ID: {os.getpid()}")
        print(f"    Environment: {env_name}")
        print(f"    Python: {sys.executable}")
        print(f"    Parameters: {params}")
    
    # Record execution info
    pipeline_data["step_local"] = {
        "executed": True,
        "environment": "local",
        "environment_name": env_name,
        "python_executable": sys.executable,
        "python_version": sys.version,
        "process_id": os.getpid(),
        "params_used": params
    }
    
    return pipeline_data
