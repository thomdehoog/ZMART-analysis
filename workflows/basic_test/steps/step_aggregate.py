"""
Benchmark step — aggregate accumulated results at a scope boundary.

Receives a list of results from prior jobs and combines them.
Used to measure scope collection and aggregation performance.
"""

METADATA = {
    "description": "Scope aggregation benchmark",
    "environment": "local",
}


def run(pipeline_data, **params):
    import time

    t0 = time.monotonic()
    results = pipeline_data.get("results", [])

    total_items = 0
    for r in results:
        gen = r.get("generated", {})
        total_items += gen.get("n_items", 0)

    elapsed = time.monotonic() - t0
    pipeline_data["aggregated"] = {
        "n_results": len(results),
        "total_items": total_items,
        "elapsed": elapsed,
    }
    return pipeline_data
