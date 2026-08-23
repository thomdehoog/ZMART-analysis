"""
image_io — Image loading shared by the workflow steps.

OME-Zarr is read with ngio, which covers NGFF 0.4 and 0.5 behind a single
API: one Zarr per position, axes TCZYX. Plain image files (OME-TIFF, TIFF,
PNG, ...) and skimage sample datasets are also accepted.

Reads stay lazy. Only the chunks backing the requested plane are fetched,
so a large sharded position costs one plane of memory, not the whole
TCZYX array. Projections reduce over z with dask and never materialise the
full stack either.

The analysis steps are 2D, so loading always returns a single YX plane.
"""


def is_ome_zarr(source) -> bool:
    """True if `source` looks like a Zarr store rather than an image file."""
    from pathlib import Path

    text = str(source)

    if text.startswith(("s3://", "gs://", "http://", "https://")):
        return ".zarr" in text.lower()

    path = Path(text)
    return path.is_dir() and (
        (path / "zarr.json").exists() or (path / ".zattrs").exists()
    )


def _open_position(source):
    """
    Open one OME-Zarr position.

    Pointing at a plate or well instead of a position is the easy mistake
    to make, so it gets its own message with the positions listed.
    """
    import ngio

    try:
        return ngio.open_ome_zarr_container(str(source), mode="r", cache=True)
    except ngio.NgioValidationError:
        try:
            paths = ngio.open_ome_zarr_plate(str(source)).images_paths()
        except Exception:
            raise

        listed = ", ".join(paths[:8]) + (" ..." if len(paths) > 8 else "")
        raise ValueError(
            f"{source} is an OME-Zarr plate, not a position. This workflow "
            f"reads one Zarr per position, so point data_source at one of "
            f"them, for example {str(source).rstrip('/')}/{paths[0]}\n"
            f"Positions: {listed}"
        ) from None


def _load_ome_zarr(source, level, t, c, z):
    """Load one YX plane from an OME-Zarr position."""
    container = _open_position(source)
    image = container.get_image(path=str(level))

    projection = str(z).lower() if isinstance(z, str) else None
    projection = projection if projection in ("max", "mean") else None

    slicing = {}
    if image.has_axis("t"):
        slicing["t"] = int(t)
    if image.has_axis("z") and projection is None:
        if z is None or str(z).lower() == "mid":
            slicing["z"] = image.dimensions.get("z") // 2
        else:
            slicing["z"] = int(z)

    project_z = projection is not None and image.has_axis("z")
    axes_order = ("z", "y", "x") if project_z else ("y", "x")
    channel = c if image.has_axis("c") else None

    plane = image.get_as_dask(
        channel_selection=channel, axes_order=axes_order, **slicing
    )

    if project_z:
        # Reduce lazily, then cast back so downstream steps keep the
        # dtype they would see for a single plane.
        plane = getattr(plane, projection)(axis=0).astype(image.dtype)

    plane = plane.compute()

    channel_labels = image.channel_labels
    channel_index = container.get_channel_idx(c) if isinstance(c, str) else int(c)

    metadata = {
        "source": str(source),
        "format": "ome-zarr",
        "ngff_version": str(container.meta.version),
        "axes": list(image.axes),
        "shape": list(image.shape),
        "chunks": list(image.chunks),
        "dtype": str(image.dtype),
        "level": str(level),
        "n_levels": container.levels,
        "index": {k: int(v) for k, v in slicing.items()},
        "projection": projection,
        "channel": channel_index if image.has_axis("c") else None,
        "channel_name": (channel_labels[channel_index]
                         if image.has_axis("c") and channel_labels else None),
        "pixel_size": dict(zip(image.axes, image.dataset.scale)),
        "origin": dict(zip(image.axes, image.dataset.translation)),
        "space_unit": image.space_unit,
    }

    return plane, metadata


def _basic_metadata(source, fmt, image):
    """Metadata for inputs that carry no NGFF spatial information."""
    return {
        "source": str(source),
        "format": fmt,
        "ngff_version": None,
        "axes": ["y", "x"],
        "shape": [int(s) for s in image.shape],
        "dtype": str(image.dtype),
        "index": {},
        "projection": None,
        "channel": None,
        "channel_name": None,
        "pixel_size": {},
        "origin": {},
        "space_unit": None,
    }


def load_plane(source, level=0, t=0, c=0, z="mid"):
    """
    Load a single 2D YX plane.

    Parameters
    ----------
    source : str
        One of:
          * a path or URL of an OME-Zarr position (NGFF 0.4 or 0.5)
          * a path to a 2D image file, e.g. OME-TIFF
          * "skimage.<name>", e.g. "skimage.human_mitosis"
    level : int or str
        Multiscale resolution level. 0 is full resolution. OME-Zarr only.
    t : int
        Time point index. OME-Zarr only.
    c : int or str
        Channel index, or a channel label from the OMERO metadata.
        OME-Zarr only.
    z : int or str
        Z index, "mid" for the middle plane, or "max" / "mean" for a
        projection along z. OME-Zarr only.

    Returns
    -------
    (numpy.ndarray, dict)
        The plane, and metadata describing where it came from.
    """
    import numpy as np

    text = str(source)

    if text.startswith("skimage."):
        from skimage import data as skimage_data

        loader = getattr(skimage_data, text.split(".", 1)[1], None)
        if loader is None:
            raise ValueError(f"Unknown skimage sample dataset: {text}")
        image = np.asarray(loader())
        return image, _basic_metadata(text, "skimage-sample", image)

    if is_ome_zarr(text):
        return _load_ome_zarr(text, level, t, c, z)

    from skimage.io import imread

    image = np.asarray(imread(text))
    if image.ndim != 2:
        raise ValueError(
            f"Expected a 2D image file, got shape {image.shape} from {text}. "
            f"Use OME-Zarr to pick a plane out of a multi-dimensional stack."
        )
    return image, _basic_metadata(text, "image-file", image)


def to_physical(centroid_y, centroid_x, metadata):
    """
    Map a pixel centroid to physical coordinates for the loaded level.

    Applies the scale and translation of the multiscale dataset, so the
    result is in stage coordinates and directly usable as microscope
    feedback. Returns None when the input carries no spatial metadata.
    """
    pixel_size = metadata.get("pixel_size") or {}
    if "y" not in pixel_size or "x" not in pixel_size:
        return None

    origin = metadata.get("origin") or {}

    return {
        "y": centroid_y * float(pixel_size["y"]) + float(origin.get("y", 0.0)),
        "x": centroid_x * float(pixel_size["x"]) + float(origin.get("x", 0.0)),
        "unit": metadata.get("space_unit"),
    }
