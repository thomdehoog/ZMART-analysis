"""The detection settings, read from the submit and the recipe.

Every setting has a default in the recipe, and a submit may override it for
one image by naming the same key in its input. That is how the operator
page tunes detection on a single position without registering a recipe of
its own. ``setting`` is that one rule, used for every key.

``segmentation_params`` gathers the settings that decide what the masks
look like, and ``segmentation_params_hash`` turns them into one short
identity. Two detections with the same hash were made the same way; the
checkpoint records it so a table can always be traced back to how it was
produced. The size and border filters are kept apart, because they are
applied after the detector and can be retuned from the saved raw masks.
"""

from __future__ import annotations

import hashlib
import json
import math

from .contract import to_builtin

#: The settings that change the masks themselves. The GPU and the filters
#: are left out on purpose: the GPU is where it ran, not what it did, and
#: the filters can be applied again to the raw masks.
SEGMENTATION_IDENTITY_KEYS = (
    "z_selection",
    "method",
    "threshold",
    "channels",
    "cellprob_threshold",
    "flow_threshold",
    "niter",
    "diameter",
    "segmentation_binning",
)


def setting(inp: dict, params: dict, key: str, default=None):
    """The value of one setting: the submit's when it names it, else the recipe's."""
    return inp[key] if key in inp else params.get(key, default)


def none_or_int(value):
    """``int(value)``, or None for None."""
    return None if value is None else int(value)


def none_or_float(value):
    """``float(value)``, or None for None."""
    return None if value is None else float(value)


def segmentation_params(inp: dict, params: dict) -> dict:
    """The settings that decide what the masks look like."""
    return {
        "z_selection": inp.get("z_selection", "mid"),
        "channels": setting(inp, params, "channels"),
        "method": setting(inp, params, "method") or "robust",
        "threshold": none_or_float(setting(inp, params, "threshold")),
        "cellprob_threshold": setting(inp, params, "cellprob_threshold"),
        "flow_threshold": setting(inp, params, "flow_threshold"),
        "niter": setting(inp, params, "niter"),
        "diameter": setting(inp, params, "diameter"),
        "segmentation_binning": setting(inp, params, "segmentation_binning"),
    }


def segmentation_params_hash(params: dict) -> str:
    """One short identity for the way a segmentation was made.

    Only the keys in ``SEGMENTATION_IDENTITY_KEYS`` count. A key that is
    missing and a key that is None hash the same, so an older checkpoint
    and a newer one agree when nothing that matters changed.
    """
    identity = {key: params.get(key, None) for key in SEGMENTATION_IDENTITY_KEYS}
    text = json.dumps(to_builtin(identity), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def border_filter_params(inp: dict, params: dict) -> dict:
    """The overlap guard: how wide a band at each tile edge is rejected."""
    return {"border_margin_px": none_or_int(setting(inp, params, "border_margin_px"))}


def area_filter_params(inp: dict, params: dict) -> dict:
    """The size bounds, in pixels, whichever way they were given.

    A bound may be given as an area in pixels or as an equivalent diameter
    in micrometres, which survives a change of pixel size; one kind per
    side, not both. A diameter needs ``source_pixel_size_um`` to become an
    area in pixels.
    """
    min_diameter_um = setting(inp, params, "min_equivalent_diameter_um")
    max_diameter_um = setting(inp, params, "max_equivalent_diameter_um")
    pixel_size = setting(inp, params, "source_pixel_size_um")

    min_area_px = _area_bound_px(
        area_px=setting(inp, params, "min_area_px"),
        diameter_um=min_diameter_um,
        source_pixel_size_um=pixel_size,
        bound="min",
    )
    max_area_px = _area_bound_px(
        area_px=setting(inp, params, "max_area_px"),
        diameter_um=max_diameter_um,
        source_pixel_size_um=pixel_size,
        bound="max",
    )
    if min_area_px is not None and max_area_px is not None and max_area_px < min_area_px:
        raise ValueError("max object size must be >= min object size.")
    return {
        "min_area_px": min_area_px,
        "max_area_px": max_area_px,
        "min_equivalent_diameter_um": none_or_float(min_diameter_um),
        "max_equivalent_diameter_um": none_or_float(max_diameter_um),
    }


def _area_bound_px(*, area_px, diameter_um, source_pixel_size_um, bound: str):
    area_px = none_or_int(area_px)
    diameter_um = none_or_float(diameter_um)
    if area_px is not None and diameter_um is not None:
        raise ValueError(
            f"Specify either {bound}_area_px or {bound}_equivalent_diameter_um, not both."
        )
    if area_px is not None:
        if area_px < 0:
            raise ValueError(f"{bound}_area_px must be >= 0.")
        return area_px
    if diameter_um is None:
        return None
    if diameter_um < 0:
        raise ValueError(f"{bound}_equivalent_diameter_um must be >= 0.")
    pixel_area_um2 = _pixel_area_um2(source_pixel_size_um)
    area_um2 = math.pi * (diameter_um / 2.0) ** 2
    area_px_float = area_um2 / pixel_area_um2
    if bound == "min":
        return int(math.ceil(area_px_float))
    if bound == "max":
        return int(math.floor(area_px_float))
    raise ValueError("bound must be 'min' or 'max'.")


def _pixel_area_um2(source_pixel_size_um):
    if source_pixel_size_um is None:
        raise ValueError("source_pixel_size_um is required when filtering by equivalent diameter.")
    if isinstance(source_pixel_size_um, (int, float)):
        sx = sy = float(source_pixel_size_um)
    else:
        values = list(source_pixel_size_um)
        if len(values) == 0:
            raise ValueError("source_pixel_size_um cannot be empty.")
        sx = float(values[0])
        sy = float(values[1] if len(values) > 1 else values[0])
    if sx <= 0 or sy <= 0:
        raise ValueError("source_pixel_size_um values must be > 0.")
    return sx * sy
