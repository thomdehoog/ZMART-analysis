"""
Test step -- stores a value that JSON cannot carry.

Runs in env_b and puts raw bytes into pipeline_data. Used to check that a
pipeline-level environment can hand such data back to the caller when the
pipeline asks for pickle transfer, and that the engine explains the problem
clearly when it does not.
"""

METADATA = {
    "description": "Test step - stores bytes (not JSON-serialisable)",
    "version": "1.0",
    "environment": "local",
}


def run(pipeline_data: dict, **params) -> dict:
    import os
    import sys

    pipeline_data["step_bytes"] = {
        "executed": True,
        "payload": b"\x00\x01\x02smart",
        "environment_name": os.path.basename(sys.prefix),
        "process_id": os.getpid(),
    }
    return pipeline_data
