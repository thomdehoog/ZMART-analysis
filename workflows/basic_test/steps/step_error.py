"""
Test step -- deliberately raises an exception.

Used to test that the engine handles step failures gracefully.
"""

METADATA = {
    "description": "Test step that raises an error",
    "version": "1.0",
    "environment": "local",
}


def run(pipeline_data: dict, **params) -> dict:
    verbose = pipeline_data["metadata"].get("verbose", 0)

    if verbose >= 2:
        print()
        print("=" * 50)
        print("  STEP: step_error")
        print("=" * 50)
        print("    About to raise an error...")

    raise RuntimeError("Deliberate test error from step_error")
