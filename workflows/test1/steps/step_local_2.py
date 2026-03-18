"""
Test Function - Second Local Step

Another local function to test pipeline flow.

IMPORTANT: All imports must be inside the run() function
to support environment switching.
"""

METADATA = {
    "description": "Test function - second local step",
    "version": "1.0",
    "environment": "local"  # Default - runs in current process
}


def run(pipeline_data: dict, **params) -> dict:
    """Execute locally in the main process."""
    import sys
    import os
    
    env_name = os.path.basename(sys.prefix)
    verbose = pipeline_data["metadata"].get("verbose", 0)
    
    # Verify previous steps ran
    previous_steps = []
    for key in pipeline_data:
        if key not in ("metadata", "input"):
            previous_steps.append(key)
    
    # Step output (verbose levels 2 and 3)
    if verbose >= 2:
        print()
        print("=" * 50)
        print("  STEP: step_local_2")
        print("=" * 50)
        print(f"    Process ID: {os.getpid()}")
        print(f"    Environment: {env_name}")
        print(f"    Parameters: {params}")
        print(f"    Previous steps found: {previous_steps}")
    
    # Record execution info
    pipeline_data["step_local_2"] = {
        "executed": True,
        "environment": "local",
        "environment_name": env_name,
        "python_executable": sys.executable,
        "python_version": sys.version,
        "process_id": os.getpid(),
        "params_used": params,
        "previous_steps_found": previous_steps
    }
    
    return pipeline_data
