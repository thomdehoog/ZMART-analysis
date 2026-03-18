"""
Test step -- requests SMART--basic_test--env_b environment.

Used to test that step-level environment switching works.
"""

METADATA = {
    "description": "Test step - runs in env_b",
    "version": "1.0",
    "environment": "SMART--basic_test--env_b",
    "data_transfer": "file_paths",
}


def run(pipeline_data: dict, **params) -> dict:
    import sys
    import os

    env_name = os.path.basename(sys.prefix)
    verbose = pipeline_data["metadata"].get("verbose", 0)
    requested_env = METADATA["environment"]

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

    pipeline_data["step_env"] = {
        "executed": True,
        "requested_environment": requested_env,
        "actual_environment": env_name,
        "environment_match": env_name == requested_env,
        "python_executable": sys.executable,
        "process_id": os.getpid(),
        "params_used": params,
    }

    return pipeline_data
