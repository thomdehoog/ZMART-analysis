"""
Feedback — Write selected cell coordinates to JSON.

Produces a JSON file with pixel coordinates and properties
of selected cells, readable by downstream programs.
"""

METADATA = {
    "description": "Write selected cell coordinates to JSON",
    "version": "1.0",
    "environment": "local",
}


def run(pipeline_data: dict, **params) -> dict:
    import json
    import numpy as np
    from pathlib import Path
    from datetime import datetime

    verbose = pipeline_data["metadata"].get("verbose", 0)
    output_dir = params.get("output_dir", ".")

    props = pipeline_data["extract_features"]["properties"]
    selected_labels = pipeline_data["extract_features"]["selected_labels"]
    threshold = pipeline_data["extract_features"]["threshold"]
    select_by = pipeline_data["extract_features"]["select_by"]

    # Build feedback records
    cells = []
    for lbl in selected_labels:
        idx = int(np.where(props['label'] == lbl)[0][0])
        cells.append({
            "label": int(lbl),
            "centroid_x": float(props['centroid-1'][idx]),
            "centroid_y": float(props['centroid-0'][idx]),
            "area": int(props['area'][idx]),
            "mean_intensity": float(props['mean_intensity'][idx]),
            "eccentricity": float(props['eccentricity'][idx]),
        })

    feedback = {
        "datetime": datetime.now().strftime("%Y%m%d-%H%M%S"),
        "label": pipeline_data["metadata"]["label"],
        "selection_criteria": {
            "feature": select_by,
            "percentile": pipeline_data["extract_features"]["percentile"],
            "threshold": threshold,
        },
        "n_selected": len(cells),
        "n_total": int(pipeline_data["segment"]["n_cells"]),
        "cells": cells,
    }

    # Write JSON
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    filename = f"feedback_{pipeline_data['metadata']['label']}.json"
    filepath = out_path / filename

    with open(filepath, 'w') as f:
        json.dump(feedback, f, indent=2)

    if verbose >= 2:
        print(f"  [feedback] Wrote {len(cells)} cells to {filepath}")

    pipeline_data["feedback"] = {
        "filepath": str(filepath),
        "n_selected": len(cells),
        "cells": cells,
    }

    return pipeline_data
