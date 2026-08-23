"""
Feedback — Write selected cell coordinates to JSON.

Produces a JSON file with the coordinates and properties of selected
cells, readable by downstream programs. Coordinates are always given in
pixels; when the input carried OME-Zarr spatial metadata they are also
given in physical units, so the file can be fed straight back to a
microscope.
"""

METADATA = {
    "description": "Write selected cell coordinates to JSON",
    "version": "1.1",
    "environment": "local",
}


def run(pipeline_data: dict, **params) -> dict:
    import json
    import numpy as np
    from pathlib import Path
    from datetime import datetime

    # The engine puts the steps directory on sys.path
    from image_io import to_physical

    verbose = pipeline_data["metadata"].get("verbose", 0)
    output_dir = params.get("output_dir", ".")

    props = pipeline_data["extract_features"]["properties"]
    selected_labels = pipeline_data["extract_features"]["selected_labels"]
    threshold = pipeline_data["extract_features"]["threshold"]
    select_by = pipeline_data["extract_features"]["select_by"]
    image_metadata = pipeline_data["preprocess"].get("image_metadata", {})

    # Build feedback records
    cells = []
    for lbl in selected_labels:
        idx = int(np.where(props['label'] == lbl)[0][0])
        centroid_y = float(props['centroid-0'][idx])
        centroid_x = float(props['centroid-1'][idx])

        cell = {
            "label": int(lbl),
            "centroid_x": centroid_x,
            "centroid_y": centroid_y,
            "area": int(props['area'][idx]),
            "mean_intensity": float(props['mean_intensity'][idx]),
            "eccentricity": float(props['eccentricity'][idx]),
        }

        physical = to_physical(centroid_y, centroid_x, image_metadata)
        if physical:
            cell["centroid_x_physical"] = physical["x"]
            cell["centroid_y_physical"] = physical["y"]
            cell["physical_unit"] = physical["unit"]

        cells.append(cell)

    feedback = {
        "datetime": datetime.now().strftime("%Y%m%d-%H%M%S"),
        "label": pipeline_data["metadata"]["label"],
        "image": {
            k: image_metadata.get(k)
            for k in ("source", "format", "ngff_version", "level", "index",
                      "channel", "channel_name", "projection", "pixel_size",
                      "origin", "space_unit")
        },
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
