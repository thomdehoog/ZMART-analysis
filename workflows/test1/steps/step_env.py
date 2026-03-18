"""
Test Function - Specific Environment

A function that requests a specific Conda environment.
Used to test that environment switching works correctly.

IMPORTANT: All imports must be inside the run() function
to support environment switching.

NOTE: Change the environment name below to match an actual 
Conda environment on your system for real testing.
"""

METADATA = {
    "description": "Test function - runs in specific environment",
    "version": "1.0",
    # Target env_b so tests from env_a will switch
    # Tests from env_b will run locally (same env)
    # Tests from env_c will switch to env_b
    "environment": "env_b",
    "data_transfer": "file_paths"  # Default - pass file paths, not large data
}


def run(pipeline_data: dict, **params) -> dict:
    """Execute in specific environment."""
    import sys
    import os
    
    env_name = os.path.basename(sys.prefix)
    verbose = pipeline_data["metadata"].get("verbose", 0)
    requested_env = METADATA['environment']
    
    # Step output (verbose levels 2 and 3)
    if verbose >= 2:
        print()
        print("=" * 50)
        print("  STEP: step_env")
        print("=" * 50)
        print(f"    Process ID: {os.getpid()}")
        print(f"    Requested environment: {requested_env}")
        print(f"    Actual environment: {env_name}")
        print(f"    Python: {sys.executable}")
        print(f"    Parameters: {params}")
    
    # Record execution info
    pipeline_data["step_env"] = {
        "executed": True,
        "environment": requested_env,
        "requested_environment": requested_env,
        "actual_environment": env_name,
        "environment_match": env_name.lower() == requested_env.lower(),
        "python_executable": sys.executable,
        "python_version": sys.version,
        "process_id": os.getpid(),
        "params_used": params
    }
    
    return pipeline_data
