"""
Feedback — Write selected cell coordinates to JSON, and the arrays to files.

Produces a JSON file with pixel coordinates and properties of selected cells,
readable by downstream programs. As the last step, it also writes the masks
and the measurement table to files and replaces them in pipeline_data with
their paths, so the pipeline's result holds only numbers, strings and paths.
That is what lets the result travel back from the analysis environment as
plain JSON, and be read by a caller that has neither NumPy nor scikit-image.
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

    # Move the arrays out of pipeline_data and into files next to the JSON.
    run_label = pipeline_data["metadata"]["label"]
    masks_path = out_path / f"masks_{run_label}.npy"
    np.save(masks_path, pipeline_data["segment"]["masks"])
    props_path = out_path / f"properties_{run_label}.csv"
    columns = list(props.keys())
    np.savetxt(
        props_path,
        np.column_stack([np.asarray(props[c], dtype=float) for c in columns]),
        delimiter=",", header=",".join(columns), comments="",
    )

    for key in ("image", "image_preprocessed"):
        pipeline_data["preprocess"].pop(key, None)
    pipeline_data["segment"]["masks"] = str(masks_path)
    pipeline_data["extract_features"]["properties"] = str(props_path)
    pipeline_data["extract_features"]["selected_labels"] = [int(l) for l in selected_labels]

    if verbose >= 2:
        print(f"  [feedback] Wrote {len(cells)} cells to {filepath}")
        print(f"  [feedback] Masks -> {masks_path}, measurements -> {props_path}")

    pipeline_data["feedback"] = {
        "filepath": str(filepath),
        "masks_path": str(masks_path),
        "properties_path": str(props_path),
        "n_selected": len(cells),
        "cells": cells,
    }

    return pipeline_data
