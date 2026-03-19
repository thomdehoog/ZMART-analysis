"""
Test step -- verifies data survives serialization across environments.

Runs in a different environment and confirms that pipeline_data
from previous steps arrived intact.
"""

METADATA = {
    "description": "Test cross-environment data transfer",
    "version": "1.0",
    "environment": "SMART--basic_test--env_b",
}


def run(pipeline_data: dict, **params) -> dict:
    import os
    import sys

    verbose = pipeline_data["metadata"].get("verbose", 0)

    has_write_data = "step_write_data" in pipeline_data

    if verbose >= 2:
        print()
        print("=" * 50)
        print("  STEP: step_pickle")
        print("=" * 50)
        print(f"    Environment: {os.path.basename(sys.prefix)}")
        print(f"    Received step_write_data: {has_write_data}")

    pipeline_data["step_pickle"] = {
        "executed": True,
        "received_previous_data": has_write_data,
        "environment": os.path.basename(sys.prefix),
        "process_id": os.getpid(),
    }

    return pipeline_data
