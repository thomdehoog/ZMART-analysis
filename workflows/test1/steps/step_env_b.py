"""
Test Function - Requests Environment B

A function that requests a DIFFERENT environment than the pipeline.
Used to test: pipeline runs in env A, but this step switches to env B.

IMPORTANT: All imports must be inside the run() function
to support environment switching.

NOTE: Change the environment name below to a DIFFERENT Conda environment
than what you set in the pipeline YAML.
"""

METADATA = {
    "description": "Test function - runs in env B (different from pipeline env A)",
    "version": "1.0",
    # Target env_c for nested switching tests
    # When pipeline is in env_a, this step switches to env_c
    "environment": "env_c",
    "data_transfer": "file_paths"
}


def run(pipeline_data: dict, **params) -> dict:
    """Execute in environment B."""
    import sys
    import os
    
    env_name = os.path.basename(sys.prefix)
    verbose = pipeline_data["metadata"].get("verbose", 0)
    requested_env = METADATA['environment']
    
    # Step output (verbose levels 2 and 3)
    if verbose >= 2:
        print()
        print("=" * 50)
        print("  STEP: step_env_b")
        print("=" * 50)
        print(f"    Process ID: {os.getpid()}")
        print(f"    Requested environment: {requested_env}")
        print(f"    Actual environment: {env_name}")
        print(f"    Python: {sys.executable}")
    
    # Record execution info
    pipeline_data["step_env_b"] = {
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
