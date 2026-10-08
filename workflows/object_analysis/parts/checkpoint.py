"""Filing a detection: the masks, and a record of how they were made.

A detection that is only in memory is lost when the run ends. The
checkpoint writes the masks before and after filtering, and a JSON record
beside them with the settings, the hash that identifies the segmentation,
and a digest of the image and of the masks. From that record a run can be
reproduced, and the filters retuned from the raw masks, without the model
running again.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .contract import to_builtin


def write_detection_checkpoint(detection: dict, raw_masks, inp: dict, params: dict) -> dict:
    """Write the masks and the record; return the paths written, or ``{}``.

    An image inside an acquisition files itself in the ``analysis`` folder
    beside the ``data`` it came from. One kept outside an acquisition has
    no such place and writes nothing, unless the submit or the recipe
    names an ``output_dir``.
    """
    output_dir = inp.get("output_dir", params.get("output_dir", None))
    if output_dir is None:
        output_dir = analysis_dir(inp["image_path"])
    if output_dir is None:
        return {}

    import tifffile

    # Filed under the frame's own short name: the name the driver wrote,
    # minus the channel and plane, which analysis of a frame spans.
    tile_dir = Path(output_dir) / "tiles" / short_name(inp["image_path"])
    tile_dir.mkdir(parents=True, exist_ok=True)
    masks_path = tile_dir / "masks.tif"
    raw_masks_path = tile_dir / "raw_masks.tif"
    checkpoint_path = tile_dir / "detection_checkpoint.json"
    tifffile.imwrite(masks_path, detection["masks"].astype("int32"))
    tifffile.imwrite(raw_masks_path, raw_masks.astype("int32"))

    checkpoint = {
        "image_path": str(inp["image_path"]),
        "image_sha256": file_sha256(inp["image_path"]),
        "tile_id": inp["tile_id"],
        "tile_stage_xy_um": inp["tile_stage_xy_um"],
        "tile_z_um": inp.get("tile_z_um"),
        "z_selection": inp.get("z_selection", "mid"),
        **({"synthetic_pixels": inp["synthetic_pixels"]} if inp.get("synthetic_pixels") else {}),
        "source_pixel_size_um": inp["source_pixel_size_um"],
        "source_image_size_px": inp.get("source_image_size_px", detection.get("image_size_px")),
        "image_to_stage": inp["image_to_stage"],
        "segmentation_params": detection["segmentation_params"],
        "segmentation_params_hash": detection["segmentation_params_hash"],
        "n_objects": detection["n_objects"],
        "n_raw_objects": detection["n_raw_objects"],
        "dropped_labels": detection["dropped_labels"],
        "area_filter": detection["area_filter"],
        "border_filter": detection["border_filter"],
        "detector_params": detection["detector_params"],
        "segmentation_resize": detection["segmentation_resize"],
        "image_size_px": detection["image_size_px"],
        "raw_masks_path": str(raw_masks_path),
        "raw_masks_sha256": file_sha256(raw_masks_path),
        "masks_path": str(masks_path),
        "masks_sha256": file_sha256(masks_path),
    }
    checkpoint_path.write_text(
        json.dumps(to_builtin(checkpoint), indent=2) + "\n", encoding="utf-8"
    )
    return {
        "masks_tif": str(masks_path),
        "raw_masks_tif": str(raw_masks_path),
        "detection_checkpoint_json": str(checkpoint_path),
    }


#: A driver writes one file per channel and plane of a frame; this is the
#: tail that varies within one, and everything before it names the frame.
_PLANE = re.compile(r"_C\d{2}_Z\d{5}\.ome\.tiff?$", re.IGNORECASE)


def short_name(image_path: Path | str) -> str:
    """``..._T000000_C00_Z00000.ome.tiff`` -> ``..._T000000``.

    Results are filed under the frame, not one plane of it. A name outside the
    convention keeps its bare stem.
    """
    name = Path(image_path).name
    frame = _PLANE.sub("", name)
    return frame if frame != name else name.split(".")[0]


def analysis_dir(image_path: Path | str) -> Path | None:
    """The ``analysis`` folder beside the ``data`` an image came from.

    The nearest ``data`` wins. ``None`` for an image kept outside an
    acquisition, which has no folder to be filed under; that is a thing
    that happens rather than a thing that is wrong.
    """
    for folder in Path(image_path).parents:
        if folder.name == "data":
            return folder.parent / "analysis"
    return None


def file_sha256(path: str | Path) -> str:
    """A digest of what was analysed: one file, or one OME-Zarr position.

    A single file digests as its bytes. An OME-Zarr position is a folder,
    a description and its chunks, and digests as every file in it in path
    order, each one's relative name and then its bytes, so that a changed
    chunk and a moved chunk are both a different position. Opening the
    folder as if it were one file is "permission denied" on Windows and
    "is a directory" on Linux, and it once lost a whole detection at the
    moment its record was being written, after Cellpose had finished.
    """
    root = Path(path)
    digest = hashlib.sha256()
    if root.is_dir():
        files = sorted(one for one in root.rglob("*") if one.is_file())
        for file in files:
            digest.update(file.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            _digest_the_bytes_of(file, digest)
    else:
        _digest_the_bytes_of(root, digest)
    return digest.hexdigest()


def _digest_the_bytes_of(file: Path, digest) -> None:
    with file.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
