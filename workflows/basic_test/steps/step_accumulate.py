"""
Scoped step -- sums a "value" key from accumulated results.

Receives the results list at a scope boundary and produces
a total sum. Used for testing scoped aggregation behavior.
"""

METADATA = {
    "description": "Sum 'value' key across accumulated results",
    "version": "1.0",
    "environment": "local",
}


def run(pipeline_data: dict, **params) -> dict:
    verbose = pipeline_data.get("metadata", {}).get("verbose", 0)

    results = pipeline_data.get("results", [])
    total = 0
    for r in results:
        total += r.get("value", 0)

    if verbose >= 2:
        print()
        print("=" * 50)
        print("  STEP: step_accumulate")
        print("=" * 50)
        print(f"    Received {len(results)} results")
        print(f"    Sum of 'value': {total}")

    pipeline_data["step_accumulate"] = {
        "executed": True,
        "n_results": len(results),
        "total": total,
    }

    return pipeline_data
