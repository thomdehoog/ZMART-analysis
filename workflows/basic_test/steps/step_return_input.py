"""
Test step -- returns pipeline_data unmodified (identity step).

Useful for testing that data passthrough does not break the pipeline
data flow between steps.
"""

METADATA = {
    "description": "Identity step - returns data unmodified",
    "version": "1.0",
    "environment": "local",
}


def run(pipeline_data: dict, **params) -> dict:
    verbose = pipeline_data["metadata"].get("verbose", 0)

    if verbose >= 2:
        print()
        print("=" * 50)
        print("  STEP: step_return_input")
        print("=" * 50)
        print(f"    Passing through {len(pipeline_data)} top-level keys")

    return pipeline_data
