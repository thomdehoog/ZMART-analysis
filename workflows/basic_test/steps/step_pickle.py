"""
Test step -- uses pickle transfer mode for complex objects.

Runs in a different environment and transfers data via pickle
instead of JSON serialization.
"""

METADATA = {
    "description": "Test pickle data transfer",
    "version": "1.0",
    "environment": "SMART--basic_test--env_b",
    "data_transfer": "pickle",
}


def run(pipeline_data: dict, **params) -> dict:
    import os
    import sys

    verbose = pipeline_data["metadata"].get("verbose", 0)

    # Verify we received data from previous step
    has_write_data = "step_write_data" in pipeline_data

    if verbose >= 2:
        print()
        print("=" * 50)
        print("  STEP: step_pickle")
        print("=" * 50)
        print(f"    Environment: {os.path.basename(sys.prefix)}")
        print(f"    Data transfer: pickle")
        print(f"    Received step_write_data: {has_write_data}")

    pipeline_data["step_pickle"] = {
        "executed": True,
        "data_transfer": "pickle",
        "received_previous_data": has_write_data,
        "environment": os.path.basename(sys.prefix),
        "process_id": os.getpid(),
    }

    return pipeline_data
