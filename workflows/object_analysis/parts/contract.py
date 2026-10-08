"""The shape of the object table, and the check that a tile's result has it.

Everything that reads the object table, from the operator page to the
population steps, relies on the same columns being there with one value per
object. ``validate_tile_detection`` is that promise, checked once at the end
of the per-tile pipeline. ``to_builtin`` turns numpy values into the plain
Python ones that can be written as JSON and sent between workers.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

#: The columns every object table has, whatever features were measured.
REQUIRED_OBJECT_COLUMNS = (
    "label",
    "centroid_row_px",
    "centroid_col_px",
    "bbox_min_row_px",
    "bbox_min_col_px",
    "bbox_max_row_px",
    "bbox_max_col_px",
    "area",
    "intensity_mean",
    "eccentricity",
    "stage_x_um",
    "stage_y_um",
)

#: What a tile has to say about where it was taken, so that an object's
#: pixel position can be turned into a stage position.
REQUIRED_GEOMETRY_FIELDS = (
    "tile_id",
    "tile_stage_xy_um",
    "source_pixel_size_um",
    "source_image_size_px",
    "image_to_stage",
)


def to_builtin(obj: Any) -> Any:
    """A copy of ``obj`` made of plain Python values that JSON can write.

    Numpy arrays become lists and numpy scalars become Python numbers.
    Tuples become lists. A float that is not a number (NaN or infinite)
    becomes ``None``, so ``json.dump(..., allow_nan=False)`` accepts it.
    """
    if obj is None or type(obj) in (str, bool, int):
        return obj
    if type(obj) is float:
        return obj if math.isfinite(obj) else None
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, Mapping):
        return {str(key): to_builtin(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_builtin(value) for value in obj]
    if hasattr(obj, "item") and getattr(obj, "ndim", 0) == 0:
        # Only a true scalar is unwrapped with item(). A one-element numpy
        # array answers item() just as happily, and unwrapping it once
        # collapsed every column of a single-object field to a bare number,
        # so the table step died on len() of an int.
        try:
            return to_builtin(obj.item())
        except (TypeError, ValueError):
            pass
    if hasattr(obj, "tolist"):
        return to_builtin(obj.tolist())
    raise TypeError(f"Value of type {type(obj).__name__} is not JSON-native.")


def validate_tile_detection(tile: Mapping[str, Any]) -> dict:
    """Check a tile's result against the contract and return it as plain values.

    Raises ``ValueError`` with a message naming what is missing or the wrong
    shape: a required column, a column whose length is not the number of
    objects, a geometry field, or a non-JSON value.
    """
    tile = _json_checked(to_builtin(tile), "tile detection")
    if not isinstance(tile, dict):
        raise ValueError("tile detection must be a dict.")

    objects = _require_dict(tile, "objects", "tile detection")
    props = _require_dict(objects, "properties", "objects")
    n_objects = objects.get("n_objects")
    if not isinstance(n_objects, int) or isinstance(n_objects, bool) or n_objects < 0:
        raise ValueError("objects.n_objects must be a non-negative int.")

    for name in REQUIRED_OBJECT_COLUMNS:
        if name not in props:
            raise ValueError(f"objects.properties missing required column {name!r}.")

    for name, values in props.items():
        if not isinstance(values, list):
            raise ValueError(f"objects.properties[{name!r}] must be a list.")
        if len(values) != n_objects:
            raise ValueError(
                f"objects.properties[{name!r}] has length {len(values)}; "
                f"expected n_objects={n_objects}."
            )

    geometry = _require_dict(tile, "geometry", "tile detection")
    for name in REQUIRED_GEOMETRY_FIELDS:
        if name not in geometry:
            raise ValueError(f"geometry missing required field {name!r}.")

    _require_list_len(geometry, "tile_id", None, "geometry")
    _require_list_len(geometry, "tile_stage_xy_um", 2, "geometry")
    _require_list_len(geometry, "source_pixel_size_um", 2, "geometry")
    _require_list_len(geometry, "source_image_size_px", 2, "geometry")
    matrix = _require_list_len(geometry, "image_to_stage", 2, "geometry")
    for row in matrix:
        if not isinstance(row, list) or len(row) != 2:
            raise ValueError("geometry.image_to_stage must be a 2x2 list.")

    embeddings = objects.get("embeddings")
    if embeddings is not None:
        if not isinstance(embeddings, dict):
            raise ValueError("objects.embeddings must be a dict when present.")
        if "label" in embeddings:
            labels = embeddings["label"]
            if not isinstance(labels, list) or len(labels) != n_objects:
                raise ValueError("objects.embeddings.label must be a list aligned to n_objects.")
            if labels != props["label"]:
                raise ValueError("objects.embeddings.label must match objects.properties.label.")
        if "vectors" in embeddings:
            vectors = embeddings["vectors"]
            if not isinstance(vectors, list) or len(vectors) != n_objects:
                raise ValueError("objects.embeddings.vectors must be a list aligned to n_objects.")
            for idx, vector in enumerate(vectors):
                if not isinstance(vector, list):
                    raise ValueError(f"objects.embeddings.vectors[{idx}] must be a list.")

    return tile


def _json_checked(obj: Any, label: str) -> Any:
    try:
        json.dumps(obj, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is not JSON round-trippable: {exc}") from exc
    return obj


def _require_dict(parent: Mapping[str, Any], key: str, label: str) -> dict:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{label}.{key} must be a dict.")
    return value


def _require_list_len(
    parent: Mapping[str, Any], key: str, expected_len: int | None, label: str
) -> list:
    value = parent.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{label}.{key} must be a list.")
    if expected_len is not None and len(value) != expected_len:
        raise ValueError(f"{label}.{key} must have length {expected_len}.")
    return value
