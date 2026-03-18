"""
Test step -- writes specific test data to pipeline_data.

Used to verify data survives environment switching and serialization.
"""

METADATA = {
    "description": "Write test data for verification",
    "version": "1.0",
    "environment": "local",
}


def run(pipeline_data: dict, **params) -> dict:
    import os
    import sys

    verbose = pipeline_data["metadata"].get("verbose", 0)

    test_data = {
        "string": "hello_from_step_write_data",
        "integer": 42,
        "float": 3.14159,
        "boolean": True,
        "list": [1, 2, 3, "four", 5.0],
        "nested": {
            "key": "value",
            "numbers": [10, 20, 30],
        },
        "none_value": None,
        "written_by_env": os.path.basename(sys.prefix),
        "written_by_pid": os.getpid(),
    }

    if verbose >= 2:
        print()
        print("=" * 50)
        print("  STEP: step_write_data")
        print("=" * 50)
        print(f"    Wrote {len(test_data)} keys to pipeline_data")

    pipeline_data["step_write_data"] = test_data

    return pipeline_data
