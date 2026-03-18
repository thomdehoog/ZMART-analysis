"""
Segment — Run Cellpose v4 (CPSAM) segmentation.

Reads preprocessed image from pipeline_data, returns label masks.
"""

METADATA = {
    "description": "Cellpose v4 CPSAM segmentation",
    "version": "1.0",
    "environment": "local",
}


def run(pipeline_data: dict, **params) -> dict:
    from cellpose import models

    verbose = pipeline_data["metadata"].get("verbose", 0)
    diameter = params.get("diameter", None)
    gpu = params.get("gpu", False)

    img_pre = pipeline_data["preprocess"]["image_preprocessed"]

    model = models.CellposeModel(gpu=gpu)
    masks, flows, styles = model.eval(img_pre, diameter=diameter)

    n_cells = int(masks.max())

    if verbose >= 2:
        print(f"  [segment] Cells found: {n_cells}")
        print(f"  [segment] diameter={diameter}, gpu={gpu}")

    pipeline_data["segment"] = {
        "masks": masks,
        "n_cells": n_cells,
        "diameter": diameter,
    }

    return pipeline_data
