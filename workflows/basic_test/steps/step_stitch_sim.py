"""
Scoped step -- stitches accumulated tile results into a grid summary.

Receives the results list from tile processing at a scope boundary,
combines them into a stitched grid with dimensions and value totals.
"""

METADATA = {
    "description": "Stitch accumulated tile results into grid summary",
    "version": "1.0",
    "environment": "local",
}


def run(pipeline_data: dict, **params) -> dict:
    verbose = pipeline_data.get("metadata", {}).get("verbose", 0)

    results = pipeline_data.get("results", [])

    tiles = []
    rows = set()
    cols = set()
    total_value = 0

    for r in results:
        tile = r.get("tile_result", {})
        tiles.append(tile)
        rows.add(tile.get("row", 0))
        cols.add(tile.get("col", 0))
        total_value += tile.get("value", 0)

    if verbose >= 2:
        print()
        print("=" * 50)
        print("  STEP: step_stitch_sim")
        print("=" * 50)
        print(f"    Stitching {len(tiles)} tiles")
        print(f"    Grid: {len(rows)} rows x {len(cols)} cols")
        print(f"    Total value: {total_value}")

    pipeline_data["stitched"] = {
        "n_tiles": len(tiles),
        "n_rows": len(rows),
        "n_cols": len(cols),
        "total_value": total_value,
        "tiles": tiles,
    }

    return pipeline_data
