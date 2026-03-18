"""
Test step -- runs locally (no environment switching).
"""

METADATA = {
    "description": "Test step - runs locally",
    "version": "1.0",
    "environment": "local",
}


def run(pipeline_data: dict, **params) -> dict:
    import sys
    import os

    env_name = os.path.basename(sys.prefix)
    verbose = pipeline_data["metadata"].get("verbose", 0)

    if verbose >= 2:
        print()
        print("=" * 50)
        print("  STEP: step_local")
        print("=" * 50)
        print(f"    Process ID: {os.getpid()}")
        print(f"    Environment: {env_name}")
        print(f"    Python: {sys.executable}")
        print(f"    Parameters: {params}")

    pipeline_data["step_local"] = {
        "executed": True,
        "environment": "local",
        "environment_name": env_name,
        "python_executable": sys.executable,
        "process_id": os.getpid(),
        "params_used": params,
    }

    return pipeline_data
