"""
Scoped step -- produces a tracking summary from temporal results.

Receives accumulated timepoint results at a temporal scope boundary
and produces a summary with trajectory information.
"""

METADATA = {
    "description": "Tracking summary from temporal scope results",
    "version": "1.0",
    "environment": "local",
}


def run(pipeline_data: dict, **params) -> dict:
    verbose = pipeline_data.get("metadata", {}).get("verbose", 0)

    results = pipeline_data.get("results", [])

    timepoints = []
    values = []

    for r in results:
        tp = r.get("timepoint", r.get("input", {}).get("timepoint", None))
        val = r.get("value", r.get("input", {}).get("value", 0))
        timepoints.append(tp)
        values.append(val)

    n_points = len(timepoints)
    mean_value = sum(values) / n_points if n_points > 0 else 0.0
    value_range = (min(values), max(values)) if values else (0, 0)

    if verbose >= 2:
        print()
        print("=" * 50)
        print("  STEP: step_track_sim")
        print("=" * 50)
        print(f"    Tracking {n_points} timepoints")
        print(f"    Mean value: {mean_value:.2f}")
        print(f"    Range: {value_range}")

    pipeline_data["tracking"] = {
        "n_timepoints": n_points,
        "timepoints": timepoints,
        "values": values,
        "mean_value": mean_value,
        "value_range": list(value_range),
    }

    return pipeline_data
