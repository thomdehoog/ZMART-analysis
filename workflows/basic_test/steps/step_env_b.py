"""
Test step -- requests SMART--basic_test--env_c environment.

Used to test nested environment switching (pipeline in env_a,
this step switches to env_c).
"""

METADATA = {
    "description": "Test step - runs in env_c",
    "version": "1.0",
    "environment": "SMART--basic_test--env_c",
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
        print("  STEP: step_env_b")
        print("=" * 50)
        print(f"    Process ID: {os.getpid()}")
        print(f"    Requested environment: {requested_env}")
        print(f"    Actual environment: {env_name}")
        print(f"    Python: {sys.executable}")

    pipeline_data["step_env_b"] = {
        "executed": True,
        "requested_environment": requested_env,
        "actual_environment": env_name,
        "environment_match": env_name == requested_env,
        "python_executable": sys.executable,
        "process_id": os.getpid(),
        "params_used": params,
    }

    return pipeline_data
