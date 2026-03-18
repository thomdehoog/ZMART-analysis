"""
Test step -- second local step to test data flow between steps.
"""

METADATA = {
    "description": "Test step - second local step",
    "version": "1.0",
    "environment": "local",
}


def run(pipeline_data: dict, **params) -> dict:
    import sys
    import os

    env_name = os.path.basename(sys.prefix)
    verbose = pipeline_data["metadata"].get("verbose", 0)

    previous_steps = [k for k in pipeline_data if k not in ("metadata", "input")]

    if verbose >= 2:
        print()
        print("=" * 50)
        print("  STEP: step_local_2")
        print("=" * 50)
        print(f"    Process ID: {os.getpid()}")
        print(f"    Environment: {env_name}")
        print(f"    Parameters: {params}")
        print(f"    Previous steps found: {previous_steps}")

    pipeline_data["step_local_2"] = {
        "executed": True,
        "environment": "local",
        "environment_name": env_name,
        "python_executable": sys.executable,
        "process_id": os.getpid(),
        "params_used": params,
        "previous_steps_found": previous_steps,
    }

    return pipeline_data
