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
    """
    True if `source` looks like a Zarr store rather than an image file.

    Local stores are recognised by their layout, remote ones by suffix.
    Reading a remote store also needs the matching fsspec driver
    installed, s3fs for s3:// or gcsfs for gs://, which the workflow
    environment does not install by default.
    """
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

    Pointing at a plate or a well instead of a position is the easy
    mistake to make, so those get their own message with the positions
    listed. Any other failure is raised untouched.
    """
    import ngio

    try:
        return ngio.open_ome_zarr_container(str(source), mode="r", cache=True)
    except ngio.NgioValidationError as error:
        kind, paths = _positions_below(source)
        if not paths:
            raise error

        listed = ", ".join(paths[:8]) + (" ..." if len(paths) > 8 else "")
        raise ValueError(
            f"{source} is an OME-Zarr {kind}, not a position. This workflow "
            f"reads one Zarr per position, so point data_source at one of "
            f"them, for example {str(source).rstrip('/')}/{paths[0]}\n"
            f"Positions: {listed}"
        ) from error


def _positions_below(source):
    """(kind, position paths) if `source` is a plate or a well, else (None, [])."""
    import ngio

    for kind, opener, lister in (
        ("plate", ngio.open_ome_zarr_plate, lambda g: g.images_paths()),
        ("well", ngio.open_ome_zarr_well, lambda g: g.paths()),
    ):
        try:
            return kind, list(lister(opener(str(source))))
        except Exception:
            continue

    return None, []


def _load_ome_zarr(source, level, t, c, z):
    """Load one YX plane from an OME-Zarr position."""
    import numpy as np

    container = _open_position(source)
    image = container.get_image(path=str(level))

    projection = _projection_mode(z) if image.has_axis("z") else None

    slicing = {}
    if image.has_axis("t"):
        slicing["t"] = int(t)
    if image.has_axis("z") and projection is None:
        slicing["z"] = _z_index(z, image.dimensions.get("z"))

    axes_order = ("z", "y", "x") if projection else ("y", "x")
    channel = c if image.has_axis("c") else None

    plane = image.get_as_dask(
        channel_selection=channel, axes_order=axes_order, **slicing
    )

    if projection:
        # Reduce lazily, then cast back so downstream steps keep the
        # dtype they would see for a single plane. Integer dtypes are
        # rounded rather than truncated.
        plane = getattr(plane, projection)(axis=0)
        if np.dtype(image.dtype).kind in "iu":
            plane = plane.round()
        plane = plane.astype(image.dtype)

    plane = plane.compute()

    if image.has_axis("c"):
        channel_index = (container.get_channel_idx(c) if isinstance(c, str)
                         else int(c))
    else:
        channel_index = None
    channel_labels = image.channel_labels

    metadata = {
        "source": str(source),
        "format": "ome-zarr",
        "ngff_version": str(container.meta.version),
        "axes": list(image.axes),
        "shape": list(image.shape),
        "dtype": str(image.dtype),
        "level": str(level),
        "index": {k: int(v) for k, v in slicing.items()},
        "projection": projection,
        "channel": channel_index,
        "channel_name": (channel_labels[channel_index]
                         if channel_index is not None and channel_labels
                         else None),
        "pixel_size": dict(zip(image.axes, image.dataset.scale)),
        "origin": dict(zip(image.axes, image.dataset.translation)),
        "space_unit": image.space_unit,
    }

    return plane, metadata


def _projection_mode(z):
    """The projection z asks for, or None if it names a single plane."""
    if isinstance(z, str) and z.lower() in ("max", "mean"):
        return z.lower()
    return None


def _z_index(z, n_z):
    """Resolve z to a plane index."""
    if z is None or (isinstance(z, str) and z.lower() == "mid"):
        return n_z // 2
    try:
        return int(z)
    except (TypeError, ValueError):
        raise ValueError(
            f"Unknown z selection {z!r}. Use an index, \"mid\", or a "
            f"projection: \"max\" or \"mean\"."
        ) from None


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
          * a path or URL of an OME-Zarr position (NGFF 0.4 or 0.5).
            A URL needs the matching fsspec driver installed.
          * a path to a 2D image file, e.g. OME-TIFF
          * "skimage.<name>", e.g. "skimage.human_mitosis"
    level : int or str
        Multiscale resolution level, matched against the dataset paths in
        the multiscale metadata, which are "0", "1", ... by convention.
        OME-Zarr only.
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

        name = text.split(".", 1)[1]
        loader = getattr(skimage_data, name, None)
        if not callable(loader) or name.startswith("_"):
            raise ValueError(f"Unknown skimage sample dataset: {name}")
        image = _require_2d(np.asarray(loader()), text)
        return image, _basic_metadata(text, "skimage-sample", image)

    if is_ome_zarr(text):
        return _load_ome_zarr(text, level, t, c, z)

    from skimage.io import imread

    image = _require_2d(np.asarray(imread(text)), text)
    return image, _basic_metadata(text, "image-file", image)


def _require_2d(image, source):
    """
    Reject anything that is not a single greyscale plane.

    The steps downstream are 2D, so a stack or an RGB image would fail
    later with a much less obvious error. OME-Zarr input picks its plane
    through the level / t / c / z parameters instead.
    """
    if image.ndim != 2:
        raise ValueError(
            f"Expected a single 2D plane, got shape {image.shape} from "
            f"{source}. RGB and multi-dimensional inputs are not supported "
            f"here; convert to OME-Zarr and select a plane with the "
            f"level / t / c / z parameters."
        )
    return image


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
