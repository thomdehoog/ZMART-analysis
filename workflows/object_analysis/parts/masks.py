"""Filtering and resizing label images.

A label image, or mask, is an integer picture the same size as the tile:
zero is background, and every object has its own number. The detector
numbers the objects once, and the filters here keep those numbers, so an
object that survives keeps the same id whatever bounds it was filtered
with. ``dropped_labels`` then names real objects, and a filter can be
retuned from the saved raw masks without renaming anything.
"""

from __future__ import annotations

import numpy as np

from .settings import none_or_int


def filter_masks_by_area(masks, *, min_area_px=None, max_area_px=None):
    """Drop objects smaller than ``min_area_px`` or larger than ``max_area_px``.

    Returns ``(masks, dropped_labels)``. The survivors keep their labels.
    """
    min_area_px = none_or_int(min_area_px)
    max_area_px = none_or_int(max_area_px)
    if min_area_px is not None and min_area_px < 0:
        raise ValueError("min_area_px must be >= 0.")
    if max_area_px is not None and max_area_px < 0:
        raise ValueError("max_area_px must be >= 0.")
    if min_area_px is not None and max_area_px is not None and max_area_px < min_area_px:
        raise ValueError("max_area_px must be >= min_area_px.")
    if min_area_px is None and max_area_px is None:
        return masks, []

    masks = np.asarray(masks)
    areas = np.bincount(masks.ravel())
    keep = np.ones(len(areas), dtype=bool)
    keep[0] = False
    if min_area_px is not None:
        keep &= areas >= min_area_px
    if max_area_px is not None:
        keep &= areas <= max_area_px

    present = np.flatnonzero(areas)
    return _keep_labels(masks, [int(label) for label in present if label and keep[label]])


def filter_masks_by_border(masks, *, border_margin_px=None):
    """Drop objects lying within ``border_margin_px`` pixels of the edge.

    Tiles overlap, so an object near a tile's edge is very likely to appear
    again in the neighbouring tile. Rejecting a band at every edge keeps each
    object once, and does it without either tile having to know the other
    exists, which is why a margin is preferred here over reconciling
    duplicates afterwards.

    The margin is the width of the rejected band at each edge, in pixels of
    the mask. ``None`` or ``0`` rejects nothing. Set it to about half the
    overlap and an object counted twice becomes an object counted once; set
    it wider and objects are lost from the seam instead.

    Returns ``(masks, dropped_labels)``. The survivors keep their labels.
    """
    border_margin_px = none_or_int(border_margin_px)
    if border_margin_px is not None and border_margin_px < 0:
        raise ValueError("border_margin_px must be >= 0.")
    if not border_margin_px:
        return masks, []

    masks = np.asarray(masks)
    height, width = masks.shape[:2]
    if 2 * border_margin_px >= min(height, width):
        raise ValueError(
            f"border_margin_px={border_margin_px} leaves no interior in a {height}x{width} tile."
        )

    margin = border_margin_px
    touching = set(
        np.unique(
            np.concatenate(
                [
                    masks[:margin, :].ravel(),
                    masks[-margin:, :].ravel(),
                    masks[:, :margin].ravel(),
                    masks[:, -margin:].ravel(),
                ]
            )
        ).tolist()
    )
    present = np.flatnonzero(np.bincount(masks.ravel()))
    return _keep_labels(masks, [int(label) for label in present if label and label not in touching])


def _keep_labels(masks, keep: list[int]):
    """Keep only *keep*, at their own label numbers; return (masks, dropped)."""
    present = np.flatnonzero(np.bincount(masks.ravel()))
    keep_set = set(int(k) for k in keep)
    dropped = sorted(int(label) for label in present if label and int(label) not in keep_set)
    kept = np.where(np.isin(masks, list(keep_set)), masks, 0)
    return kept.astype(np.int32, copy=False), dropped


def downsample_for_segmentation(image, *, binning=None):
    """The image at 1/``binning`` of its size, and the scale that was applied.

    Cellpose on a smaller picture is faster; the masks are brought back to
    full size afterwards with ``resize_nearest``. Pixels are averaged in
    blocks (an area average), so a bright nucleus keeps its brightness
    rather than being sampled on or off. ``binning`` of None or 1 changes
    nothing and the scale is 1.0.
    """
    binning = none_or_int(binning)
    if binning is None:
        return image, 1.0
    if binning <= 0:
        raise ValueError("segmentation_binning must be > 0.")
    if binning == 1:
        return image, 1.0
    ny, nx = image.shape[:2]
    out_shape = (
        max(1, int(round(ny / float(binning)))),
        max(1, int(round(nx / float(binning)))),
    )
    return _resize_area(image, out_shape), out_shape[0] / float(ny)


def resize_nearest(image, shape):
    """A label image resized to ``shape`` without blending labels.

    Each output pixel takes the label of the nearest input pixel, so a mask
    made on a binned picture comes back to full size with its edges where
    they were and no new labels invented between two objects.
    """
    out_ny, out_nx = [int(v) for v in shape]
    if out_ny <= 0 or out_nx <= 0:
        raise ValueError("resize shape must be positive.")

    arr = np.asarray(image)
    in_ny, in_nx = arr.shape[:2]
    rows = np.minimum((np.arange(out_ny) * in_ny // out_ny), in_ny - 1)
    cols = np.minimum((np.arange(out_nx) * in_nx // out_nx), in_nx - 1)
    if arr.ndim == 2:
        return arr[rows[:, None], cols]
    return arr[rows[:, None], cols, :]


def _resize_area(image, shape):
    """Downsample by averaging the input pixels under each output pixel."""
    out_ny, out_nx = [int(v) for v in shape]
    if out_ny <= 0 or out_nx <= 0:
        raise ValueError("resize shape must be positive.")

    arr = np.asarray(image)
    in_ny, in_nx = arr.shape[:2]
    if out_ny > in_ny or out_nx > in_nx:
        raise ValueError("area resize is downsample-only.")
    if out_ny == in_ny and out_nx == in_nx:
        return arr

    work = arr.astype(np.float32, copy=False)
    work = _area_resize_axis(work, out_ny, axis=0)
    work = _area_resize_axis(work, out_nx, axis=1)
    return work


def _area_resize_axis(arr, out_size, axis):
    arr = np.asarray(arr)
    in_size = arr.shape[axis]
    if out_size == in_size:
        return arr

    scale = in_size / float(out_size)
    out_shape = list(arr.shape)
    out_shape[axis] = out_size
    out = np.empty(out_shape, dtype=np.float32)

    for out_idx in range(out_size):
        start = out_idx * scale
        stop = (out_idx + 1) * scale
        lo = int(np.floor(start))
        hi = int(np.ceil(stop))
        weights = np.array(
            [max(0.0, min(stop, src_idx + 1) - max(start, src_idx)) for src_idx in range(lo, hi)],
            dtype=np.float32,
        )
        selector = [slice(None)] * arr.ndim
        selector[axis] = slice(lo, hi)
        segment = arr[tuple(selector)]
        weight_shape = [1] * arr.ndim
        weight_shape[axis] = len(weights)
        reduced = (segment * weights.reshape(weight_shape)).sum(axis=axis) / scale
        out_selector = [slice(None)] * arr.ndim
        out_selector[axis] = out_idx
        out[tuple(out_selector)] = reduced
    return out
