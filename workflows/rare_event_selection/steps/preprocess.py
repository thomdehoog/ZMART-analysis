"""
Preprocess — Load image and apply preprocessing.

Loads one 2D plane from an OME-Zarr position (NGFF 0.4 or 0.5), an image
file, or a skimage sample dataset. Applies Gaussian smoothing and CLAHE
normalization.
"""

METADATA = {
    "description": "Load and preprocess image",
    "version": "1.1",
    "environment": "local",
}


def run(pipeline_data: dict, **params) -> dict:
    import sys
    import numpy as np
    from pathlib import Path
    from skimage.filters import gaussian
    from skimage.exposure import equalize_adapthist

    # Sibling helper module; step files are loaded by path, not imported.
    step_dir = str(Path(__file__).parent)
    if step_dir not in sys.path:
        sys.path.insert(0, step_dir)
    from image_io import load_plane

    verbose = pipeline_data["metadata"].get("verbose", 0)
    sigma = params.get("sigma", 1.0)
    clip_limit = params.get("clip_limit", 0.03)
    data_source = pipeline_data["input"].get("data_source", "skimage.human_mitosis")

    # Plane selection, used for OME-Zarr input
    level = params.get("level", 0)
    t = params.get("t", 0)
    c = params.get("c", 0)
    z = params.get("z", "mid")

    # Load a single YX plane
    img, image_metadata = load_plane(data_source, level=level, t=t, c=c, z=z)

    # Preprocess
    img_smooth = gaussian(img, sigma=sigma)
    img_pre = equalize_adapthist(img_smooth, clip_limit=clip_limit)
    img_pre = (img_pre * 255).astype(np.uint8)

    if verbose >= 2:
        print(f"  [preprocess] Loaded: {img.shape}, dtype={img.dtype}")
        print(f"  [preprocess] Source: {image_metadata['format']} "
              f"{image_metadata['source']}")
        if image_metadata["format"] == "ome-zarr":
            print(f"  [preprocess] NGFF {image_metadata['ngff_version']}, "
                  f"axes={''.join(image_metadata['axes'])}, "
                  f"shape={tuple(image_metadata['shape'])}, "
                  f"level={image_metadata['level']}")
            print(f"  [preprocess] Plane: {image_metadata['index']}, "
                  f"c={image_metadata['channel']} "
                  f"({image_metadata['channel_name']})"
                  + (f", z-projection={image_metadata['projection']}"
                     if image_metadata["projection"] else ""))
        print(f"  [preprocess] sigma={sigma}, clip_limit={clip_limit}")

    pipeline_data["preprocess"] = {
        "image": img,
        "image_preprocessed": img_pre,
        "shape": img.shape,
        "sigma": sigma,
        "clip_limit": clip_limit,
        "data_source": data_source,
        "image_metadata": image_metadata,
    }

    return pipeline_data
