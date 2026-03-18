"""
Preprocess — Load image and apply preprocessing.

Loads from skimage sample data or a file path.
Applies Gaussian smoothing and CLAHE normalization.
"""

METADATA = {
    "description": "Load and preprocess image",
    "version": "1.0",
    "environment": "local",
}


def run(pipeline_data: dict, **params) -> dict:
    import numpy as np
    from skimage.filters import gaussian
    from skimage.exposure import equalize_adapthist

    verbose = pipeline_data["metadata"].get("verbose", 0)
    sigma = params.get("sigma", 1.0)
    clip_limit = params.get("clip_limit", 0.03)
    data_source = pipeline_data["input"].get("data_source", "skimage.human_mitosis")

    # Load image
    if data_source == "skimage.human_mitosis":
        from skimage.data import human_mitosis
        img = human_mitosis()
    else:
        from skimage.io import imread
        img = imread(data_source)

    # Preprocess
    img_smooth = gaussian(img, sigma=sigma)
    img_pre = equalize_adapthist(img_smooth, clip_limit=clip_limit)
    img_pre = (img_pre * 255).astype(np.uint8)

    if verbose >= 2:
        print(f"  [preprocess] Loaded: {img.shape}, dtype={img.dtype}")
        print(f"  [preprocess] sigma={sigma}, clip_limit={clip_limit}")

    pipeline_data["preprocess"] = {
        "image": img,
        "image_preprocessed": img_pre,
        "shape": img.shape,
        "sigma": sigma,
        "clip_limit": clip_limit,
        "data_source": data_source,
    }

    return pipeline_data
