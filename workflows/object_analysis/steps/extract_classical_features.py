"""extract_classical_features -- measure every object the detector found.

Takes the label image and the channels that ``detect_objects`` published,
and measures each object: its size and shape, how bright it is in every
channel, how far its neighbours are, and, when asked, its texture. Each
measurement is one column, one value per object, in the order of the
labels. ``build_object_table`` turns them into the table that readers
rely on.

The base set is scikit-image's ``regionprops_table``: area, perimeter,
axes, orientation, bounding box, intensity mean, min, max and standard
deviation. A few cheap ratios are always added to it (circularity, aspect
ratio, the integrated intensity). The optional families are chosen with
``extras`` in the recipe:

    intensity       global_bg, local_bg      the background around each object
    neighbourhood   neighbours               distances to and counts of neighbours
    texture         gradients, stat_texture, lbp, fft, glrlm
    morphology      rg_spread                radius of gyration

A name may be a family or one of its members, and ``all`` takes every one.
The per-object texture families (lbp, fft, glrlm) loop over every object
in Python and are the slow ones; the fast recipe leaves them out.

When the image has several channels, every intensity measurement is
reported per channel too: ``intensity_mean`` is the first channel, and
``intensity_mean_c0``, ``intensity_mean_c1``, ... are each channel by
index.

Publishes under ``pipeline_data["extract_classical_features"]``::

    properties   one column per measurement, one value per object
    n_objects    how many objects were measured
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

METADATA = {
    "description": "Per-object features (shape / intensity / neighbourhood / texture)",
    "version": "1.0",
    "max_workers": 1,
    "environment": "ZMART--object_analysis--classical",
}


# ---------------------------------------------------------------------------
# The base measurements. Most are asked of scikit-image's regionprops_table
# by name. intensity_median is not one scikit-image offers under a stable
# name, so it is measured here, and the column means the same whichever
# version of scikit-image the environment holds.
# ---------------------------------------------------------------------------

SYNTHETIC_PROPERTIES = {"intensity_median"}
INTENSITY_PROPERTIES = (
    "intensity_mean",
    "intensity_min",
    "intensity_max",
    "intensity_std",
    "intensity_median",
)

DEFAULT_PROPERTIES = [
    "label",
    "bbox",
    "centroid",
    "weighted_centroid",
    "area",
    "num_pixels",
    "area_convex",
    "equivalent_diameter_area",
    "feret_diameter_max",
    "perimeter",
    "perimeter_crofton",
    "axis_major_length",
    "axis_minor_length",
    "orientation",
    "eccentricity",
    "solidity",
    "extent",
    "intensity_mean",
    "intensity_min",
    "intensity_max",
    "intensity_std",
    "intensity_median",
]


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def run(pipeline_data: dict, state: dict, **params) -> dict:
    from skimage.measure import regionprops_table

    verbose = pipeline_data["metadata"].get("verbose", 0)

    properties = list(params.get("properties", DEFAULT_PROPERTIES))
    requested_extras = list(params.get("extras", []) or [])
    pixel_size_um = params.get("pixel_size_um")

    extras, unknown = _expand_extras(requested_extras)
    if unknown:
        raise ValueError(
            f"Unknown extras {sorted(unknown)}. "
            f"Expected groups {sorted(FEATURE_GROUPS)} or extras "
            f"{sorted(EXTRAS)}."
        )

    detection = pipeline_data["detect_objects"]
    masks = np.asarray(detection["masks"])
    img = _normalise_image_axes(detection["image"], masks.shape)

    spacing_kw = {"spacing": tuple(pixel_size_um)} if pixel_size_um else {}
    native_properties = [prop for prop in properties if prop not in SYNTHETIC_PROPERTIES]
    props = regionprops_table(
        masks, intensity_image=img, properties=native_properties, **spacing_kw
    )
    _add_synthetic_properties(props, masks, img, properties)
    _normalise_intensity_columns(props, img)

    labels = np.asarray(props.get("label", []))
    n_objects = int(len(labels))

    if n_objects > 0:
        _add_derived(props)
        if extras:
            _run_extras(props, masks, img, labels, extras, params, pixel_size_um)

    if verbose >= 2:
        print(f"  [extract_classical_features] objects: {n_objects}, columns: {sorted(props)}")

    pipeline_data["extract_classical_features"] = {
        "properties": props,
        "n_objects": n_objects,
    }
    return pipeline_data


def _add_synthetic_properties(
    props: dict, masks: np.ndarray, img: np.ndarray, properties: list[str]
) -> None:
    """Measure the properties scikit-image does not offer under a stable name."""
    if "intensity_median" not in properties:
        return

    channels = _channel_images(img)
    if channels is not None:
        for idx, channel in enumerate(channels):
            props[f"intensity_median_c{idx}"] = _intensity_medians(masks, channel)
        props["intensity_median"] = props["intensity_median_c0"]
        return

    props["intensity_median"] = _intensity_medians(masks, img)


def _intensity_medians(masks: np.ndarray, img: np.ndarray) -> np.ndarray:
    """The median intensity inside each object, in label order."""
    from skimage.measure import regionprops

    medians = []
    for region in regionprops(masks, intensity_image=img):
        if hasattr(region, "image_intensity"):
            intensity_image = region.image_intensity
        else:
            intensity_image = region.intensity_image
        values = intensity_image[region.image]
        medians.append(float(np.median(values)) if values.size else np.nan)
    return np.asarray(medians, dtype=float)


def _normalise_image_axes(img, mask_shape: tuple[int, int]) -> np.ndarray:
    """The image as one plane, or as channels-last ``(H, W, C)``, the size of the masks."""
    arr = np.asarray(img)
    if arr.ndim == 2 and arr.shape == mask_shape:
        return arr
    if arr.ndim == 3 and arr.shape[:2] == mask_shape:
        return arr
    if arr.ndim == 3 and arr.shape[1:] == mask_shape:
        return np.moveaxis(arr, 0, -1)
    raise ValueError(f"image shape {arr.shape} is not aligned to mask shape {mask_shape}.")


def _channel_images(img: np.ndarray) -> tuple[np.ndarray, ...] | None:
    """One plane per channel when the image has several; None for a single plane."""
    if np.asarray(img).ndim != 3:
        return None
    return tuple(img[..., idx] for idx in range(img.shape[-1]))


def _primary_image(img: np.ndarray) -> np.ndarray:
    """The first channel: the plane the unsuffixed intensity columns measure."""
    channels = _channel_images(img)
    return img if channels is None else channels[0]


def _normalise_intensity_columns(props: dict, img: np.ndarray) -> None:
    """Name the per-channel intensity columns the way the table promises them.

    scikit-image calls them ``name-0``, ``name-1``, ... . The table uses
    ``name`` for the first channel and ``name_c0``, ``name_c1``, ... for
    each channel by index, so a reader never has to know how many there
    were.
    """
    channels = _channel_images(img)
    if channels is None:
        return

    n_channels = len(channels)
    for base in INTENSITY_PROPERTIES:
        for idx in range(n_channels):
            skimage_key = f"{base}-{idx}"
            public_key = f"{base}_c{idx}"
            if public_key in props:
                values = props[public_key]
            elif skimage_key in props:
                values = props.pop(skimage_key)
            elif idx == 0 and base in props:
                values = props[base]
            else:
                continue
            props[public_key] = values
            if idx == 0:
                props[base] = values


# ---------------------------------------------------------------------------
# Ratios that are always added. Each one needs a few base columns and is
# added only when they are there, so a recipe that asks for fewer base
# properties gets fewer ratios and no error.
# ---------------------------------------------------------------------------


def _add_derived(props: dict) -> None:
    """A few ratios of the base measurements that are useful for gating.

    An object with a zero perimeter, a zero minor axis or a zero mean
    intensity would divide by zero; it gets NaN instead, quietly.
    """
    with np.errstate(divide="ignore", invalid="ignore"):
        if {"area", "perimeter_crofton"} <= props.keys():
            a = np.asarray(props["area"], dtype=float)
            p = np.asarray(props["perimeter_crofton"], dtype=float)
            props["circularity"] = np.where(p > 0, 4 * np.pi * a / (p * p), np.nan)

        if {"axis_major_length", "axis_minor_length"} <= props.keys():
            maj = np.asarray(props["axis_major_length"], dtype=float)
            mn = np.asarray(props["axis_minor_length"], dtype=float)
            props["aspect_ratio"] = np.where(mn > 0, maj / mn, np.nan)

        if "orientation" in props:
            # scikit-image measures the angle from the row axis, between
            # -90 and 90 degrees. Here it becomes the angle from the x axis
            # between 0 and 180: a long axis lying flat is 0 degrees, one
            # standing upright is 90.
            props["orientation_deg"] = (90.0 - np.degrees(props["orientation"])) % 180.0

        if "intensity_mean" in props:
            mean_i = np.asarray(props["intensity_mean"], dtype=float)
            # ``num_pixels`` is the count of pixels even when ``area`` is
            # in micrometres (a pixel size was given), so the total stays
            # a sum of pixel values whatever the units.
            if "num_pixels" in props:
                n_px = np.asarray(props["num_pixels"], dtype=float)
                props["intensity_total"] = mean_i * n_px
            elif "area" in props:
                props["intensity_total"] = mean_i * np.asarray(props["area"], dtype=float)
            if "intensity_std" in props:
                std_i = np.asarray(props["intensity_std"], dtype=float)
                props["intensity_cv"] = np.where(mean_i > 0, std_i / mean_i, np.nan)

        for idx in _intensity_channel_indices(props):
            mean_key = f"intensity_mean_c{idx}"
            mean_i = np.asarray(props[mean_key], dtype=float)
            if "num_pixels" in props:
                n_px = np.asarray(props["num_pixels"], dtype=float)
                props[f"intensity_total_c{idx}"] = mean_i * n_px
            elif "area" in props:
                props[f"intensity_total_c{idx}"] = mean_i * np.asarray(props["area"], dtype=float)
            std_key = f"intensity_std_c{idx}"
            if std_key in props:
                std_i = np.asarray(props[std_key], dtype=float)
                props[f"intensity_cv_c{idx}"] = np.where(mean_i > 0, std_i / mean_i, np.nan)


def _intensity_channel_indices(props: dict) -> list[int]:
    prefix = "intensity_mean_c"
    indices = []
    for key in props:
        if key.startswith(prefix):
            suffix = key[len(prefix) :]
            if suffix.isdigit():
                indices.append(int(suffix))
    return sorted(indices)


# ---------------------------------------------------------------------------
# The optional families. Each is a function that takes the context below
# and adds its columns to ``props``. ``EXTRAS`` at the bottom of the file
# lists them.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Context:
    """What every optional family is handed: the same picture, once.

    ``props`` is the only thing a family changes; it adds its columns
    there. ``masks`` is the label image and ``img`` the first channel.
    ``channel_images`` holds one plane per channel when there are several,
    so a family can add ``_c0``, ``_c1``, ... columns beside the base one.
    ``labels`` lists the objects in row order. ``slices`` is the bounding
    box of each object (``slices[label - 1]``), worked out once when any
    family asked for it. ``params`` is the recipe's parameters, in which a
    family reads its own keys. ``pixel_size_um`` is ``(dy, dx)`` when a
    pixel size was given, so distances can be in micrometres.
    """

    props: dict
    masks: np.ndarray
    img: np.ndarray
    channel_images: tuple[np.ndarray, ...] | None
    labels: np.ndarray
    slices: list | None
    params: dict
    pixel_size_um: tuple | list | None


def _run_extras(props, masks, img, labels, extras, params, pixel_size_um) -> None:
    """Build the context once, then run each family asked for.

    They run in a fixed order (family, then name) so two runs add their
    columns the same way. The order does not change any value: no family
    reads another's columns, only the base measurements.
    """
    from scipy.ndimage import find_objects

    needs_bbox = any(EXTRAS[name].needs_bbox for name in extras)
    slices = find_objects(masks) if needs_bbox else None

    ctx = _Context(
        props=props,
        masks=masks,
        img=_primary_image(img),
        channel_images=_channel_images(img),
        labels=labels,
        slices=slices,
        params=params,
        pixel_size_um=pixel_size_um,
    )

    for name in sorted(extras, key=lambda n: (EXTRAS[n].family, n)):
        EXTRAS[name].handler(ctx)


def _expand_extras(extras):
    """The families asked for, with each family name opened into its members.

    Returns ``(names, unknown)``: the members to run, and any name that is
    neither a family nor a member. Families and members may be mixed.
    """
    expanded: set[str] = set()
    unknown: set[str] = set()
    for name in extras:
        if name in FEATURE_GROUPS:
            expanded |= FEATURE_GROUPS[name]
        elif name in EXTRAS:
            expanded.add(name)
        else:
            unknown.add(name)
    return expanded, unknown


# ---------------------------------------------------------------------------
# Helpers shared across families.
# ---------------------------------------------------------------------------


def _texture_scale(img: np.ndarray, masks: np.ndarray, params: dict) -> float:
    """The intensity that maps to the top texture bin.

    Texture features quantise the image into a few grey levels, and the
    number an object gets depends on where the top of the scale sits. It
    used to sit at the brightest pixel of the tile, so one hot pixel or one
    speck of debris re-binned every object on that tile, and no two tiles
    binned alike. Now it is ``intensity_scale`` from the recipe when given
    (the camera's full range is the natural choice: 4095 for 12-bit data),
    and otherwise the 99.9th percentile of the pixels inside objects, which
    a single bright pixel cannot move.
    """
    given = params.get("intensity_scale")
    if given is not None and float(given) > 0:
        return float(given)
    arr = np.asarray(img, dtype=np.float64)
    inside = arr[masks > 0] if masks is not None and np.any(masks > 0) else arr.ravel()
    scale = float(np.percentile(inside, 99.9)) if inside.size else 1.0
    return scale if scale > 0 else 1.0


def _to_uint8(img: np.ndarray, scale: float) -> np.ndarray:
    """The image as 8-bit, with *scale* at 255, for the texture measures
    that need 8-bit input (the local binary pattern). Values above the
    scale are clipped."""
    arr = np.asarray(img)
    return np.clip(arr.astype(np.float64) / scale * 255.0, 0, 255).astype(np.uint8)


def _per_label_mean(
    values_image: np.ndarray, label_image: np.ndarray, labels: np.ndarray
) -> np.ndarray:
    """The mean of ``values_image`` inside each object.

    Done in two passes over the whole image (a sum per label and a count
    per label, both with ``np.bincount``) rather than one scan of the
    image per object, which is what makes it fast on a tile with
    thousands of objects.
    """
    n_lbl = int(label_image.max()) + 1
    flat_lbl = label_image.ravel()
    flat_v = values_image.ravel().astype(np.float64)
    sums = np.bincount(flat_lbl, weights=flat_v, minlength=n_lbl)
    counts = np.bincount(flat_lbl, minlength=n_lbl)
    out = np.full(len(labels), np.nan, dtype=np.float64)
    valid = counts[labels] > 0
    out[valid] = sums[labels][valid] / counts[labels][valid]
    return out


def _bbox_padded(slice_pair, pad: int, shape) -> tuple:
    """An object's bounding box widened by ``pad`` pixels on every side,
    kept inside the image. For the families that need a ring of context
    around the object, such as the local background collar."""
    sy, sx = slice_pair
    return (
        slice(max(0, sy.start - pad), min(shape[0], sy.stop + pad)),
        slice(max(0, sx.start - pad), min(shape[1], sx.stop + pad)),
    )


def _assign_columns(props: dict, values: dict[str, np.ndarray], suffix: str = "") -> None:
    """Add the columns to ``props``, with ``suffix`` on each name."""
    for key, value in values.items():
        props[f"{key}{suffix}"] = value


def _assign_channelised(ctx: _Context, compute: Callable[[np.ndarray], dict]) -> None:
    """Add the columns for the first channel, then ``_cN`` ones for every channel."""
    _assign_columns(ctx.props, compute(ctx.img))
    if ctx.channel_images is None:
        return
    for idx, image in enumerate(ctx.channel_images):
        _assign_columns(ctx.props, compute(image), suffix=f"_c{idx}")


# ===========================================================================
# Family: Intensity
# ===========================================================================


def _global_bg_mean(ctx: _Context) -> None:
    """The mean of every pixel that belongs to no object.

    One number for the whole tile, repeated on every row so it can be
    selected, sorted or compared like any other column. NaN when every
    pixel is inside an object.

    Adds:
        bg_global_mean
    """
    bg_pixels = ctx.img[ctx.masks == 0]
    scalar = float(bg_pixels.mean()) if bg_pixels.size else float("nan")
    ctx.props["bg_global_mean"] = np.full(len(ctx.labels), scalar)
    if ctx.channel_images is not None:
        for idx, image in enumerate(ctx.channel_images):
            bg_pixels = image[ctx.masks == 0]
            scalar = float(bg_pixels.mean()) if bg_pixels.size else float("nan")
            ctx.props[f"bg_global_mean_c{idx}"] = np.full(len(ctx.labels), scalar)


def _local_bg_collar(ctx: _Context) -> None:
    """The background right around each object, from a collar.

    The collar is the ring of pixels within ``bg_radius`` of the object
    that belong to no object. It is built inside the object's bounding
    box, widened by the radius: the object's holes are filled first (the
    hole in a ring is not background), the filled shape is grown by the
    radius, the object itself is taken out, and so is any pixel of a
    neighbouring object the ring reached into.

    Adds:
        bg_local_mean         per-object collar mean
        mean_minus_local_bg   intensity_mean - bg_local_mean
        mean_over_local_bg    intensity_mean / bg_local_mean
        total_minus_local_bg  (intensity_mean - bg_local_mean) * num_pixels
        total_over_local_bg   intensity_total / bg_local_mean
    """
    bg_local = _local_bg_values(ctx.img, ctx.masks, ctx.labels, ctx.slices, ctx.params)
    ctx.props["bg_local_mean"] = bg_local
    _add_local_bg_derivatives(ctx.props, bg_local)

    if ctx.channel_images is not None:
        for idx, image in enumerate(ctx.channel_images):
            bg_local = _local_bg_values(image, ctx.masks, ctx.labels, ctx.slices, ctx.params)
            suffix = f"_c{idx}"
            ctx.props[f"bg_local_mean{suffix}"] = bg_local
            _add_local_bg_derivatives(ctx.props, bg_local, suffix=suffix)


def _local_bg_values(
    img: np.ndarray,
    masks: np.ndarray,
    labels: np.ndarray,
    slices: list,
    params: dict,
) -> np.ndarray:
    from scipy.ndimage import binary_fill_holes
    from skimage.morphology import dilation, disk

    radius = int(params.get("bg_radius", 5))
    bg_local = np.full(len(labels), np.nan, dtype=np.float64)
    footprint = disk(radius)

    for i, lab in enumerate(labels):
        sl = slices[int(lab) - 1]
        if sl is None:
            continue
        sp = _bbox_padded(sl, radius + 1, masks.shape)
        crop_lab = masks[sp]
        obj = crop_lab == lab
        filled = binary_fill_holes(obj)
        collar = dilation(filled, footprint=footprint) & ~filled & (crop_lab == 0)
        if collar.any():
            bg_local[i] = float(img[sp][collar].mean())
    return bg_local


def _add_local_bg_derivatives(props: dict, bg_local: np.ndarray, suffix: str = "") -> None:
    """The four intensity columns corrected for the local background.

    An object whose collar is zero gets NaN for the ratios instead of a
    division warning.
    """
    with np.errstate(divide="ignore", invalid="ignore"):
        mean_key = f"intensity_mean{suffix}"
        if mean_key in props:
            mean_i = np.asarray(props[mean_key], dtype=float)
            props[f"mean_minus_local_bg{suffix}"] = mean_i - bg_local
            props[f"mean_over_local_bg{suffix}"] = np.where(bg_local > 0, mean_i / bg_local, np.nan)
            n_px_key = (
                "num_pixels" if "num_pixels" in props else ("area" if "area" in props else None)
            )
            if n_px_key is not None:
                n_px = np.asarray(props[n_px_key], dtype=float)
                props[f"total_minus_local_bg{suffix}"] = (mean_i - bg_local) * n_px
        total_key = f"intensity_total{suffix}"
        if total_key in props:
            tot = np.asarray(props[total_key], dtype=float)
            props[f"total_over_local_bg{suffix}"] = np.where(bg_local > 0, tot / bg_local, np.nan)


# ===========================================================================
# Family: Neighbourhood (cKDTree on object centroids)
# ===========================================================================


def _neighbour_features(ctx: _Context) -> None:
    """How close each object is to the others.

    Distances and radii are in the units of the centroids: pixels, or
    micrometres when ``pixel_size_um`` was given.

    Adds:
        nn_distance              distance to the closest other object
        nn{K}_mean_distance      mean distance to the K nearest others
                                 (column name reflects ``neighbour_k``,
                                 default ``K = 5``)
        neighbours_within_{R}    one column per radius in
                                 ``neighbour_radii`` (default
                                 ``[5, 50, 250]``); each holds the count of
                                 other objects within ``R`` units.

    With fewer than two objects every distance is NaN and every count is
    zero; nothing fails.
    """
    from scipy.spatial import cKDTree

    k = int(ctx.params.get("neighbour_k", 5))
    radii = list(ctx.params.get("neighbour_radii", [5, 50, 250]))
    n = len(ctx.labels)

    nn_dist = np.full(n, np.nan, dtype=np.float64)
    nnk_mean = np.full(n, np.nan, dtype=np.float64)
    counts_per_radius = {r: np.zeros(n, dtype=np.int64) for r in radii}

    have_centroids = "centroid-0" in ctx.props and "centroid-1" in ctx.props
    if have_centroids and n >= 2:
        pts = np.stack(
            [
                np.asarray(ctx.props["centroid-0"], dtype=float),
                np.asarray(ctx.props["centroid-1"], dtype=float),
            ],
            axis=1,
        )
        tree = cKDTree(pts)
        # Two neighbours are asked for because the closest one is the point itself.
        d, _ = tree.query(pts, k=2)
        nn_dist = d[:, 1]
        # A small population has fewer than K others; ask for what there is.
        k_query = min(k + 1, n)
        if k_query > 1:
            d_k, _ = tree.query(pts, k=k_query)
            nnk_mean = d_k[:, 1:].mean(axis=1)
        for r in radii:
            counts_per_radius[r] = np.array(
                [len(x) - 1 for x in tree.query_ball_point(pts, r=float(r))],
                dtype=np.int64,
            )

    ctx.props["nn_distance"] = nn_dist
    ctx.props[f"nn{k}_mean_distance"] = nnk_mean
    for r in radii:
        ctx.props[f"neighbours_within_{r}"] = counts_per_radius[r]


# ===========================================================================
# Family: Texture
# ===========================================================================


def _gradient_means(ctx: _Context) -> None:
    """How much the intensity changes across each object, on average.

    Two edge filters (Prewitt and Roberts) are run once over the whole
    image, and the result is averaged inside each object.

    Adds:
        prewitt_magnitude_mean
        roberts_magnitude_mean
    """
    _assign_channelised(
        ctx,
        lambda image: _gradient_values(image, ctx.masks, ctx.labels),
    )


def _gradient_values(
    img: np.ndarray, masks: np.ndarray, labels: np.ndarray
) -> dict[str, np.ndarray]:
    from skimage.filters import prewitt, roberts

    return {
        "prewitt_magnitude_mean": _per_label_mean(prewitt(img), masks, labels),
        "roberts_magnitude_mean": _per_label_mean(roberts(img), masks, labels),
    }


def _statistical_texture(ctx: _Context) -> None:
    """The shape of each object's intensity histogram.

    The intensities are quantised into ``n_intensity_bins`` levels
    (default 256) and a histogram is made per object, all of them in one
    pass. From each histogram come four numbers: how uniform it is, its
    entropy, its skewness and its kurtosis.

    Adds:
        intensity_uniformity   sum(p^2)              ("Angular Second Moment")
        intensity_entropy      -sum(p log2 p)        (Shannon, base 2)
        intensity_skewness     m3 / m2^(3/2)         (third standardised moment)
        intensity_kurtosis     m4 / m2^2 - 3         (Fisher excess kurtosis)
    """
    _assign_channelised(
        ctx,
        lambda image: _statistical_texture_values(image, ctx.masks, ctx.labels, ctx.params),
    )


def _statistical_texture_values(
    img: np.ndarray, masks: np.ndarray, labels: np.ndarray, params: dict
) -> dict[str, np.ndarray]:
    n_bins = int(params.get("n_intensity_bins", 256))
    img_arr = np.asarray(img)
    vmax = _texture_scale(img_arr, masks, params)
    img_q = np.clip(img_arr / vmax * (n_bins - 1), 0, n_bins - 1).astype(np.int64)

    fg = masks > 0
    label_to_row = np.full(int(masks.max()) + 1, -1, dtype=np.int64)
    label_to_row[labels] = np.arange(len(labels))

    rows = label_to_row[masks[fg]]
    vals = img_q[fg]
    valid = rows >= 0
    rows = rows[valid]
    vals = vals[valid]

    counts = (
        np.bincount(rows * n_bins + vals, minlength=len(labels) * n_bins)
        .reshape(len(labels), n_bins)
        .astype(np.float64)
    )
    n_per = counts.sum(axis=1)
    p = np.divide(counts, n_per[:, None], out=np.zeros_like(counts), where=n_per[:, None] > 0)

    levels = np.arange(n_bins, dtype=np.float64)
    uniformity = (p * p).sum(axis=1)
    logp = np.zeros_like(p)
    np.log2(p, out=logp, where=p > 0)
    entropy = -(p * logp).sum(axis=1)

    mean = (p * levels).sum(axis=1)
    centered = levels[None, :] - mean[:, None]
    m2 = (p * centered**2).sum(axis=1)
    m3 = (p * centered**3).sum(axis=1)
    m4 = (p * centered**4).sum(axis=1)

    skewness = np.full(len(labels), np.nan)
    kurtosis = np.full(len(labels), np.nan)
    nz = m2 > 0
    skewness[nz] = m3[nz] / (m2[nz] ** 1.5)
    kurtosis[nz] = m4[nz] / (m2[nz] ** 2) - 3.0

    return {
        "intensity_uniformity": uniformity,
        "intensity_entropy": entropy,
        "intensity_skewness": skewness,
        "intensity_kurtosis": kurtosis,
    }


def _lbp_features(ctx: _Context) -> None:
    """Six numbers describing the local binary pattern inside each object.

    The local binary pattern codes each pixel by which of its neighbours
    are brighter than it; it is computed once over the whole image, after
    quantisation to 8 bits, so a pixel at an object's edge also sees the
    neighbours just outside. That is the usual convention.

    Adds:
        lbp_mean, lbp_std, lbp_energy, lbp_entropy,
        lbp_skewness, lbp_kurtosis
    """
    _assign_channelised(
        ctx,
        lambda image: _lbp_values(image, ctx.masks, ctx.labels, ctx.slices, ctx.params),
    )


def _lbp_values(
    img: np.ndarray,
    masks: np.ndarray,
    labels: np.ndarray,
    slices: list,
    params: dict,
) -> dict[str, np.ndarray]:
    from scipy.stats import kurtosis as sstat_kurt
    from scipy.stats import skew as sstat_skew
    from skimage.feature import local_binary_pattern
    from skimage.measure import shannon_entropy

    P = int(params.get("lbp_P", 8))
    R = float(params.get("lbp_R", 1))
    method = str(params.get("lbp_method", "default"))

    lbp = local_binary_pattern(
        _to_uint8(img, _texture_scale(img, masks, params)), P=P, R=R, method=method
    ).astype(np.int32)

    n = len(labels)
    out = np.full((n, 6), np.nan, dtype=np.float64)
    for i, lab in enumerate(labels):
        sl = slices[int(lab) - 1]
        if sl is None:
            continue
        v = lbp[sl][masks[sl] == lab]
        if v.size == 0:
            continue
        out[i, 0] = v.mean()
        out[i, 1] = v.std()
        h = np.bincount(v.astype(np.int64))
        if h.sum() > 0:
            p = h / h.sum()
            out[i, 2] = float((p * p).sum())
        out[i, 3] = float(shannon_entropy(v))
        if v.size > 1 and v.std() > 0:
            out[i, 4] = float(sstat_skew(v))
            out[i, 5] = float(sstat_kurt(v, fisher=True, bias=True))

    return {
        key: out[:, col]
        for col, key in enumerate(
            (
                "lbp_mean",
                "lbp_std",
                "lbp_energy",
                "lbp_entropy",
                "lbp_skewness",
                "lbp_kurtosis",
            )
        )
    }


def _fft_features(ctx: _Context) -> None:
    """Six numbers describing each object's frequency content.

    Each object's bounding box, with the background set to zero, goes
    through a 2-D Fourier transform; the numbers describe the spread of
    the magnitudes. The entropy is taken over a histogram of
    ``fft_entropy_bins`` bins, because binning floats by unique value
    (what ``skimage.measure.shannon_entropy`` does) would make every
    object look alike.

    No windowing or padding is applied, so the high-frequency power of
    two objects of different sizes is not directly comparable.

    Adds:
        fft_mean, fft_std, fft_energy, fft_entropy,
        fft_skewness, fft_kurtosis
    """
    _assign_channelised(
        ctx,
        lambda image: _fft_values(image, ctx.masks, ctx.labels, ctx.slices, ctx.params),
    )


def _fft_values(
    img: np.ndarray,
    masks: np.ndarray,
    labels: np.ndarray,
    slices: list,
    params: dict,
) -> dict[str, np.ndarray]:
    from scipy.stats import kurtosis as sstat_kurt
    from scipy.stats import skew as sstat_skew

    n_bins = int(params.get("fft_entropy_bins", 256))
    n = len(labels)
    out = np.full((n, 6), np.nan, dtype=np.float64)

    for i, lab in enumerate(labels):
        sl = slices[int(lab) - 1]
        if sl is None:
            continue
        m = masks[sl] == lab
        if not m.any():
            continue
        crop = img[sl].astype(np.float64) * m
        F = np.abs(np.fft.fftshift(np.fft.fft2(crop)))
        flat = F.ravel()
        out[i, 0] = float(flat.mean())
        out[i, 1] = float(flat.std())
        out[i, 2] = float((F * F).sum())
        hist, _ = np.histogram(F, bins=n_bins)
        s = hist.sum()
        if s > 0:
            p = hist / s
            nz = p > 0
            out[i, 3] = float(-(p[nz] * np.log2(p[nz])).sum())
        if flat.size > 1 and flat.std() > 0:
            out[i, 4] = float(sstat_skew(flat))
            out[i, 5] = float(sstat_kurt(flat, fisher=True, bias=True))

    return {
        key: out[:, col]
        for col, key in enumerate(
            (
                "fft_mean",
                "fft_std",
                "fft_energy",
                "fft_entropy",
                "fft_skewness",
                "fft_kurtosis",
            )
        )
    }


def _glrlm_features(ctx: _Context) -> None:
    """Four gray-level run-length features, summed over four directions.

    A run is a stretch of pixels in a line with the same gray level, after
    quantising to ``glrlm_levels`` levels (default 16). Pixels outside the
    object are marked so a run never continues into the background or a
    neighbour. The runs are counted along rows, columns and both
    diagonals, and the four numbers summarise the count table: how uneven
    the run lengths are, how much dark and bright runs weigh, and how
    uneven the gray levels are.

    Gray levels count from one in the formulas, so the darkest level does
    not divide by zero.

    Adds:
        glrlm_rlnu   run length non-uniformity     sum_r (sum_g P)^2 / TR
        glrlm_lglre  low gray level run emphasis   sum_{g,r} P / g^2 / TR
        glrlm_hglre  high gray level run emphasis  sum_{g,r} P * g^2 / TR
        glrlm_glnu   gray level non-uniformity     sum_g (sum_r P)^2 / TR
    """
    _assign_channelised(
        ctx,
        lambda image: _glrlm_values(image, ctx.masks, ctx.labels, ctx.slices, ctx.params),
    )


def _glrlm_values(
    img: np.ndarray,
    masks: np.ndarray,
    labels: np.ndarray,
    slices: list,
    params: dict,
) -> dict[str, np.ndarray]:
    n_levels = int(params.get("glrlm_levels", 16))
    img_arr = np.asarray(img)
    vmax = _texture_scale(img_arr, masks, params)
    img_q = np.clip(img_arr.astype(np.float64) / vmax * (n_levels - 1), 0, n_levels - 1).astype(
        np.int16
    )

    n = len(labels)
    out = np.full((n, 4), np.nan, dtype=np.float64)
    g = np.arange(1, n_levels + 1, dtype=np.float64)[:, None]

    for i, lab in enumerate(labels):
        sl = slices[int(lab) - 1]
        if sl is None:
            continue
        m = masks[sl] == lab
        if not m.any():
            continue
        crop_q_obj = np.where(m, img_q[sl], -1).astype(np.int16)
        P = _glrlm_matrix_4dir(crop_q_obj, n_levels).astype(np.float64)
        TR = float(P.sum())
        if TR == 0:
            continue
        sum_g = P.sum(axis=0)  # over gray levels -> per run length
        sum_r = P.sum(axis=1)  # over run lengths -> per gray level
        out[i] = [
            float((sum_g**2).sum() / TR),
            float((P / (g**2)).sum() / TR),
            float((P * (g**2)).sum() / TR),
            float((sum_r**2).sum() / TR),
        ]

    return {
        key: out[:, col]
        for col, key in enumerate(
            (
                "glrlm_rlnu",
                "glrlm_lglre",
                "glrlm_hglre",
                "glrlm_glnu",
            )
        )
    }


def _runs_in_line(line: np.ndarray):
    """The runs in one line of pixels: ``(gray_levels, run_lengths)``.

    Pixels marked negative (the background mark set outside the object)
    are dropped after the runs are found, so no run crosses the object's
    edge.
    """
    if line.size == 0:
        return np.empty(0, dtype=line.dtype), np.empty(0, dtype=np.int64)
    diff = np.diff(line, prepend=line[0] - 1, append=line[-1] + 1)
    idx = np.flatnonzero(diff)
    starts = idx[:-1]
    lengths = np.diff(idx).astype(np.int64)
    vals = line[starts]
    valid = vals >= 0
    return vals[valid], lengths[valid]


def _glrlm_matrix_4dir(crop_q: np.ndarray, n_levels: int) -> np.ndarray:
    """The run count table summed over 0, 45, 90 and 135 degrees.

    Row ``g`` is a gray level and column ``r`` a run length of ``r + 1``.
    """
    H, W = crop_q.shape
    if H == 0 or W == 0:
        return np.zeros((n_levels, 1), dtype=np.int64)
    P = np.zeros((n_levels, max(H, W)), dtype=np.int64)

    def _accumulate(lines):
        for line in lines:
            vals, lens = _runs_in_line(line)
            if vals.size:
                np.add.at(P, (vals, lens - 1), 1)

    _accumulate(crop_q)  # 0
    _accumulate(crop_q.T)  # 90
    _accumulate(np.diagonal(crop_q, k) for k in range(-H + 1, W))  # 45
    _accumulate(np.diagonal(np.fliplr(crop_q), k) for k in range(-H + 1, W))  # 135
    return P


# ===========================================================================
# Family: Morphology
# ===========================================================================


def _radius_of_gyration_and_spread(ctx: _Context) -> None:
    """How spread out each object is, and whether its brightness sits at the centre.

    The radius of gyration is the root mean square distance of the
    object's pixels from its centre. The radial variance compares where
    the intensity sits with where the pixels sit: 1 for an object that is
    evenly bright, below 1 when the brightness is concentrated at the
    centre, above 1 when it sits at the rim.

    For an evenly bright disc of radius R::

        radius_of_gyration                         = R / sqrt(2)
        intensity_radial_variance_normalised       = 1

    Adds:
        radius_of_gyration                       Rg = sqrt( (1/N) sum r_i^2 )
        intensity_radial_variance_normalised     sum( I_i * r_i^2 )
                                                  / ( mean(I) * N * Rg^2 )
    """
    dy, dx = ctx.pixel_size_um if ctx.pixel_size_um else (1.0, 1.0)
    n = len(ctx.labels)
    rg = np.full(n, np.nan, dtype=np.float64)
    spread = np.full(n, np.nan, dtype=np.float64)

    for i, lab in enumerate(ctx.labels):
        sl = ctx.slices[int(lab) - 1]
        if sl is None:
            continue
        sy, sx = sl
        crop_lab = ctx.masks[sl]
        obj = crop_lab == lab
        if not obj.any():
            continue
        ys, xs = np.where(obj)
        ys = (ys + sy.start) * dy
        xs = (xs + sx.start) * dx
        cm_y = ys.mean()
        cm_x = xs.mean()
        d2 = (ys - cm_y) ** 2 + (xs - cm_x) ** 2
        rg_v = float(np.sqrt(d2.mean()))
        rg[i] = rg_v
        u = ctx.img[sl][obj].astype(np.float64)
        u_mean = float(u.mean())
        N = u.size
        if rg_v > 0 and u_mean > 0:
            spread[i] = float((u * d2).sum() / (u_mean * N * rg_v**2))

    ctx.props["radius_of_gyration"] = rg
    ctx.props["intensity_radial_variance_normalised"] = spread


# ===========================================================================
# The list of optional families: which exist, which family each belongs
# to, and which need the per-object bounding boxes. Everything else (the
# recipe's ``extras``, the error message for an unknown name, the family
# names) is derived from this one table.
#
# To add a measurement: write a function above that takes the context and
# adds its columns, then add a line here.
# ===========================================================================


@dataclass(frozen=True)
class _Extra:
    handler: Callable[[_Context], None]
    family: str
    needs_bbox: bool


EXTRAS: dict[str, _Extra] = {
    "global_bg": _Extra(_global_bg_mean, "intensity", False),
    "local_bg": _Extra(_local_bg_collar, "intensity", True),
    "neighbours": _Extra(_neighbour_features, "neighbourhood", False),
    "gradients": _Extra(_gradient_means, "texture", False),
    "stat_texture": _Extra(_statistical_texture, "texture", False),
    "lbp": _Extra(_lbp_features, "texture", True),
    "fft": _Extra(_fft_features, "texture", True),
    "glrlm": _Extra(_glrlm_features, "texture", True),
    "rg_spread": _Extra(_radius_of_gyration_and_spread, "morphology", True),
}


FEATURE_GROUPS: dict[str, set[str]] = {
    family: {name for name, e in EXTRAS.items() if e.family == family}
    for family in {e.family for e in EXTRAS.values()}
}
FEATURE_GROUPS["all"] = set(EXTRAS)
