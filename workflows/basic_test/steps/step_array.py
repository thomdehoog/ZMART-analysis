"""
Test step -- stores a NumPy array.

Used to check that a pipeline running in another environment can hand a real
array back to the caller with pickle transfer. That works only when the
caller has NumPy too; the test skips when it does not.
"""

METADATA = {
    "description": "Test step - stores a NumPy array",
    "version": "1.0",
    "environment": "local",
}


def run(pipeline_data: dict, **params) -> dict:
    import os
    import sys

    import numpy as np

    pipeline_data["step_array"] = {
        "executed": True,
        "array": np.arange(12, dtype=np.float32).reshape(3, 4),
        "environment_name": os.path.basename(sys.prefix),
        "process_id": os.getpid(),
    }
    return pipeline_data
