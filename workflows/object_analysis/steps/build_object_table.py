"""build_object_table -- the object table every reader of this workflow relies on.

The last per-tile step. It takes the features the previous step measured,
names the columns the way the table promises them (``centroid-0`` becomes
``centroid_row_px``, and so on), adds where each object sits on the stage
and a stable name for it, and checks the whole against the contract in
``parts/contract.py``. Then it drops the image and the masks from the
result, since the table is what travels on; ``keep_intermediate: true``
keeps them.

Publishes under ``pipeline_data["object_analysis"]``::

    objects     properties (one list per column, one entry per object) and
                n_objects
    geometry    the tile's id, stage position, pixel size, size in pixels,
                and the matrix from image axes to stage axes
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from object_analysis.parts.contract import to_builtin, validate_tile_detection  # noqa: E402
from object_analysis.parts.settings import setting  # noqa: E402

METADATA = {
    "description": "Build the public object_analysis table",
    "version": "1.0",
    "max_workers": 1,
}

#: The feature columns renamed for the table, from what scikit-image calls
#: them to what the table promises. Every other column keeps its name.
FEATURE_COLUMN_MAP = {
    "label": "label",
    "centroid-0": "centroid_row_px",
    "centroid-1": "centroid_col_px",
    "bbox-0": "bbox_min_row_px",
    "bbox-1": "bbox_min_col_px",
    "bbox-2": "bbox_max_row_px",
    "bbox-3": "bbox_max_col_px",
    "area": "area",
    "intensity_mean": "intensity_mean",
    "eccentricity": "eccentricity",
}


def run(pipeline_data: dict, state: dict, **params) -> dict:
    props = pipeline_data["extract_classical_features"]["properties"]

    public_props = _map_feature_columns(props)
    n_objects = len(public_props.get("label", []))
    geometry = _geometry_from_input(
        pipeline_data["input"],
        pipeline_data.get("detect_objects", {}),
    )
    _add_stage_columns(public_props, geometry)
    _add_identity_columns(public_props, geometry)

    tile_detection = validate_tile_detection(
        {
            "objects": {"properties": public_props, "n_objects": n_objects},
            "geometry": geometry,
        }
    )

    pipeline_data["object_analysis"] = tile_detection
    if not setting(pipeline_data["input"], params, "keep_intermediate", False):
        _strip_heavy_intermediates(pipeline_data)
    return pipeline_data


def _map_feature_columns(props: dict) -> dict:
    public_props = {}
    for source, public in FEATURE_COLUMN_MAP.items():
        if source not in props:
            raise ValueError(
                f"extract_classical_features output missing required column {source!r} "
                f"for public column {public!r}."
            )
        public_props[public] = to_builtin(props[source])

    mapped_sources = set(FEATURE_COLUMN_MAP)
    for name, values in props.items():
        if name not in mapped_sources and name not in public_props:
            public_props[name] = to_builtin(values)
    return public_props


def _add_stage_columns(props: dict, geometry: dict) -> None:
    stage_x = []
    stage_y = []
    for row in range(len(props["label"])):
        x_um, y_um = image_point_to_stage_xy(
            centroid_row_px=props["centroid_row_px"][row],
            centroid_col_px=props["centroid_col_px"][row],
            image_size_px=geometry["source_image_size_px"],
            pixel_size_um=geometry["source_pixel_size_um"],
            tile_stage_xy_um=geometry["tile_stage_xy_um"],
            image_to_stage=geometry["image_to_stage"],
        )
        stage_x.append(float(x_um))
        stage_y.append(float(y_um))
    props["stage_x_um"] = stage_x
    props["stage_y_um"] = stage_y


def _add_identity_columns(props: dict, geometry: dict) -> None:
    t_name = tile_name(geometry["tile_id"])
    props["tile_name"] = [t_name for _ in props["label"]]
    props["object_id"] = [object_name(geometry["tile_id"], int(label)) for label in props["label"]]


def _geometry_from_input(inp: dict, detection: dict) -> dict:
    required = [
        "tile_id",
        "tile_stage_xy_um",
        "source_pixel_size_um",
        "image_to_stage",
    ]
    for name in required:
        if name not in inp:
            raise ValueError(f"input missing required geometry field {name!r}.")

    return {
        "tile_id": inp["tile_id"],
        "tile_stage_xy_um": inp["tile_stage_xy_um"],
        # The height the tile was captured at, in the frame the acquisition
        # reports z in. It is provenance, and optional: a source that does
        # not know it says so rather than writing a made-up zero.
        "tile_z_um": inp.get("tile_z_um"),
        "source_pixel_size_um": inp["source_pixel_size_um"],
        "source_image_size_px": inp.get("source_image_size_px", detection.get("image_size_px")),
        "image_to_stage": inp["image_to_stage"],
    }


def _strip_heavy_intermediates(pipeline_data: dict) -> None:
    """Drop the image, the masks and the raw features: the table carries on alone."""
    pipeline_data.pop("extract_classical_features", None)
    detection = pipeline_data.get("detect_objects")
    if isinstance(detection, dict):
        detection.pop("image", None)
        detection.pop("masks", None)


def image_point_to_stage_xy(
    *,
    centroid_row_px,
    centroid_col_px,
    image_size_px,
    pixel_size_um,
    tile_stage_xy_um,
    image_to_stage,
) -> tuple[float, float]:
    """Where a point of the image is on the stage, in micrometres.

    ``image_size_px`` is ``[nx, ny]`` and ``pixel_size_um`` is
    ``[pixel_width_um, pixel_height_um]``. The point's offset from the
    image centre, in micrometres, is turned by the 2x2 ``image_to_stage``
    matrix (image rows and columns do not have to run the same way as the
    stage's x and y) and added to the tile's own stage position.
    """
    nx, ny = image_size_px
    pixel_width_um, pixel_height_um = pixel_size_um
    tile_x_um, tile_y_um = tile_stage_xy_um
    m00, m01 = image_to_stage[0]
    m10, m11 = image_to_stage[1]

    offset_x_um = (float(centroid_col_px) - float(nx) / 2.0) * float(pixel_width_um)
    offset_y_um = (float(centroid_row_px) - float(ny) / 2.0) * float(pixel_height_um)

    stage_x_um = float(tile_x_um) + float(m00) * offset_x_um + float(m01) * offset_y_um
    stage_y_um = float(tile_y_um) + float(m10) * offset_x_um + float(m11) * offset_y_um
    return stage_x_um, stage_y_um


def tile_name(tile_id) -> str:
    """A stable, file-name-friendly name for a tile: ``R0_r003_c007``."""
    parts = list(tile_id)
    if len(parts) >= 3:
        region = _slug(parts[0])
        row = _format_axis("r", parts[1])
        col = _format_axis("c", parts[2])
        rest = [_slug(part) for part in parts[3:]]
        return "_".join([region, row, col] + rest)
    return "_".join(_slug(part) for part in parts)


def object_name(tile_id, label: int) -> str:
    """A stable name for one object: its tile's name and its label."""
    return f"{tile_name(tile_id)}_obj{int(label):05d}"


def _format_axis(prefix: str, value) -> str:
    try:
        return f"{prefix}{int(value):03d}"
    except (TypeError, ValueError):
        return f"{prefix}{_slug(value)}"


def _slug(value) -> str:
    text = str(value)
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-")
    return text or "item"
