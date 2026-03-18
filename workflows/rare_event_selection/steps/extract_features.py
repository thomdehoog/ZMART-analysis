"""
Extract Features — Measure cell properties and select by criteria.

Runs regionprops on masks + original image, then selects cells
by a percentile threshold on a chosen feature.
"""

METADATA = {
    "description": "Feature extraction and cell selection",
    "version": "1.0",
    "environment": "local",
}


def run(pipeline_data: dict, **params) -> dict:
    import numpy as np
    from skimage.measure import regionprops_table

    verbose = pipeline_data["metadata"].get("verbose", 0)
    select_by = params.get("select_by", "area")
    percentile = params.get("percentile", 99)

    masks = pipeline_data["segment"]["masks"]
    img = pipeline_data["preprocess"]["image"]

    # Measure properties
    props = regionprops_table(masks, intensity_image=img, properties=[
        'label', 'area', 'centroid', 'eccentricity',
        'mean_intensity', 'max_intensity', 'solidity',
        'major_axis_length', 'minor_axis_length',
    ])

    # Select by percentile threshold
    values = props[select_by]
    threshold = float(np.percentile(values, percentile))
    selected_mask = values >= threshold
    selected_labels = props['label'][selected_mask]

    if verbose >= 2:
        print(f"  [extract_features] Measured {len(props['label'])} cells")
        print(f"  [extract_features] Selection: {select_by} >= {threshold:.0f} "
              f"(p{percentile}) -> {len(selected_labels)} cells")

    pipeline_data["extract_features"] = {
        "properties": props,
        "select_by": select_by,
        "percentile": percentile,
        "threshold": threshold,
        "selected_labels": selected_labels,
    }

    return pipeline_data
