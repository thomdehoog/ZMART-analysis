"""
Test step -- simulates tile processing.

Takes row/col from input data and produces a tile_result dict with
coordinates and a computed value. For testing scoped spatial pipelines.
"""

METADATA = {
    "description": "Simulate tile processing with row/col",
    "version": "1.0",
    "environment": "local",
}


def run(pipeline_data: dict, **params) -> dict:
    import os
    import sys

    verbose = pipeline_data["metadata"].get("verbose", 0)
    input_data = pipeline_data.get("input", {})

    row = input_data.get("row", 0)
    col = input_data.get("col", 0)
    computed_value = (row + 1) * 100 + (col + 1)

    if verbose >= 2:
        print()
        print("=" * 50)
        print("  STEP: step_tile_sim")
        print("=" * 50)
        print(f"    Tile ({row}, {col}) -> value {computed_value}")

    pipeline_data["tile_result"] = {
        "row": row,
        "col": col,
        "value": computed_value,
        "environment": os.path.basename(sys.prefix),
        "process_id": os.getpid(),
    }

    return pipeline_data
