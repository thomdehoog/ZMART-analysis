"""detect_objects -- Cellpose object detection for one image tile."""

from __future__ import annotations

import hashlib
import json
import math
import operator
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from shared.image_io import (  # noqa: E402
    load_channels,
    load_plane,
)

METADATA = {
    "description": "Detect objects in a TIFF tile with Cellpose",
    "version": "1.0",
    "max_workers": 1,
    "environment": "ZMART--object_analysis--cellpose",
}


def run(pipeline_data: dict, state: dict, **params) -> dict:
    verbose = pipeline_data.get("metadata", {}).get("verbose", 0)
    inp = pipeline_data["input"]
    seg_params = segmentation_params(inp, params)
    area_params = area_filter_params(inp, params)
    border_params = border_filter_params(inp, params)
    gpu = inp.get("gpu", params.get("gpu", True))
    detection = segment_position(
        inp["image_path"],
        state,
        z=inp.get("z_selection", "mid"),
        method=seg_params["method"],
        threshold=seg_params["threshold"],
        channels=seg_params["channels"],
        channel_axis=seg_params["channel_axis"],
        border_margin_px=border_params["border_margin_px"],
        min_area_px=area_params["min_area_px"],
        max_area_px=area_params["max_area_px"],
        cellprob_threshold=seg_params["cellprob_threshold"],
        flow_threshold=seg_params["flow_threshold"],
        niter=seg_params["niter"],
        diameter=seg_params["diameter"],
        segmentation_binning=seg_params["segmentation_binning"],
        gpu=gpu,
        verbose=verbose,
        log_prefix="detect_objects",
    )
    detection["area_filter"] = area_params
    detection["border_filter"] = border_params
    raw_masks = detection.pop("raw_masks")
    detection["segmentation_params"] = seg_params
    detection["segmentation_params_hash"] = segmentation_params_hash(seg_params)
    artifacts = _write_detection_checkpoint(detection, raw_masks, inp, params)
    if artifacts:
        detection["artifacts"] = artifacts

    if bool(params.get("persist_only", False)):
        detection = _slim_detection_result(detection)
        pipeline_data["detect_objects"] = detection
        return pipeline_data

    pipeline_data["detect_objects"] = detection
    # Bridge to the shared classical feature extractor's current input contract.
    # Extra channels ride along channel-last, so intensity features come out
    # per colour (name, name_c0, name_c1, ...); segmentation itself stays on
    # the single image it was handed.
    image = detection["image"]
    extra_paths = inp.get("extra_channel_paths") or []
    extra_channels = inp.get("extra_channel_indices") or []
    if extra_paths:
        from tifffile import imread

        planes = [np.asarray(image)]
        planes += [np.asarray(imread(str(path))) for path in extra_paths]
        image = np.stack(planes, axis=-1)
    elif extra_channels:
        # The multichannel case when the input is one store rather than one
        # file per plane: the other colours live in the same image, so they
        # are read from it by index -- the same lazy per-channel read the
        # segmenter uses -- and stacked channel-last exactly like the paths.
        planes = [np.asarray(image)]
        planes += [
            np.asarray(
                load_plane(inp["image_path"], c=int(channel), z=inp.get("z_selection", "mid"))[0]
            )
            for channel in extra_channels
        ]
        image = np.stack(planes, axis=-1)
    pipeline_data["preprocess"] = {"image": image}
    pipeline_data["segment"] = {
        "masks": detection["masks"],
        "n_cells": detection["n_objects"],
    }
    return pipeline_data


def _write_detection_checkpoint(detection: dict, raw_masks, inp: dict, params: dict) -> dict:
    output_dir = inp.get("output_dir", params.get("output_dir", None))
    # An image inside an acquisition files itself, in the `analysis` folder
    # beside the `data` it came from. One kept outside an acquisition has no
    # such place, and writes nothing unless the caller says where.
    if output_dir is None:
        output_dir = analysis_dir(inp["image_path"])
    if output_dir is None:
        return {}

    import tifffile

    # Filed under the frame's own short name -- the name the driver wrote,
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


def _slim_detection_result(detection: dict) -> dict:
    """Return checkpoint metadata without large image/mask arrays."""
    return {
        key: value for key, value in detection.items() if key not in {"image", "image_2d", "masks"}
    }


#: A driver writes one file per C and Z of a frame; this is the tail that
#: varies within one, and everything before it names the frame.
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
    acquisition, which has no folder to be filed under and is a thing that
    happens rather than a thing that is wrong.
    """
    for folder in Path(image_path).parents:
        if folder.name == "data":
            return folder.parent / "analysis"
    return None


SEGMENTATION_IDENTITY_KEYS = (
    "z_selection",
    "method",
    "threshold",
    "channels",
    "channel_axis",
    "cellprob_threshold",
    "flow_threshold",
    "niter",
    "diameter",
    "segmentation_binning",
)


def segmentation_params(inp: dict, params: dict) -> dict:
    """Return params that define mask generation, excluding runtime details."""
    return {
        "z_selection": inp.get("z_selection", "mid"),
        "channels": inp.get("channels", params.get("channels", None)),
        "channel_axis": _channel_axis(inp.get("channel_axis", params.get("channel_axis", None))),
        # From the submission when it names one, else the pipeline's default.
        # That is what lets an operator tune detection on a single position
        # and see the answer without re-registering a pipeline.
        "method": _setting(inp, params, "method") or "robust",
        "threshold": _none_or_float(_setting(inp, params, "threshold")),
        "cellprob_threshold": _setting(inp, params, "cellprob_threshold"),
        "flow_threshold": _setting(inp, params, "flow_threshold"),
        "niter": _setting(inp, params, "niter"),
        "diameter": _setting(inp, params, "diameter"),
        "segmentation_binning": _setting(inp, params, "segmentation_binning"),
    }


def border_filter_params(inp: dict, params: dict) -> dict:
    """The overlap guard: how wide a band at each tile edge is rejected."""
    return {"border_margin_px": _none_or_int(_setting(inp, params, "border_margin_px"))}


def area_filter_params(inp: dict, params: dict) -> dict:
    """Return post-segmentation object-size filtering params."""
    min_area_px = _setting(inp, params, "min_area_px")
    max_area_px = _setting(inp, params, "max_area_px")
    min_diameter_um = _setting(inp, params, "min_equivalent_diameter_um")
    max_diameter_um = _setting(inp, params, "max_equivalent_diameter_um")

    min_area_px = _area_bound_px(
        area_px=min_area_px,
        diameter_um=min_diameter_um,
        source_pixel_size_um=_setting(inp, params, "source_pixel_size_um"),
        bound="min",
    )
    max_area_px = _area_bound_px(
        area_px=max_area_px,
        diameter_um=max_diameter_um,
        source_pixel_size_um=_setting(inp, params, "source_pixel_size_um"),
        bound="max",
    )
    if min_area_px is not None and max_area_px is not None and max_area_px < min_area_px:
        raise ValueError("max object size must be >= min object size.")
    return {
        "min_area_px": min_area_px,
        "max_area_px": max_area_px,
        "min_equivalent_diameter_um": _none_or_float(min_diameter_um),
        "max_equivalent_diameter_um": _none_or_float(max_diameter_um),
    }


def _setting(inp: dict, params: dict, key: str):
    return inp.get(key, params.get(key, None))


def _area_bound_px(*, area_px, diameter_um, source_pixel_size_um, bound: str):
    area_px = _none_or_int(area_px)
    diameter_um = _none_or_float(diameter_um)
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


def _none_or_int(value):
    if value is None:
        return None
    return int(value)


def _none_or_float(value):
    if value is None:
        return None
    return float(value)


def _channel_axis(value):
    """Validate and normalize equivalent channel-last axis declarations."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"channel_axis must be 0, 2, -1, or None; got {value}.")
    try:
        value = operator.index(value)
    except TypeError as exc:
        raise ValueError(f"channel_axis must be 0, 2, -1, or None; got {value}.") from exc
    if value == 0:
        return 0
    if value in (-1, 2):
        return -1
    raise ValueError(f"channel_axis must be 0, 2, -1, or None; got {value}.")


def segmentation_params_hash(params: dict) -> str:
    """Stable hash of true segmentation identity params.

    This deliberately excludes GPU/CPU placement and area filters. GPU is an
    execution detail, while area filters are applied after Cellpose and can be
    retuned from persisted raw masks.
    """
    identity = {key: params.get(key, None) for key in SEGMENTATION_IDENTITY_KEYS}
    identity["channel_axis"] = _channel_axis(identity["channel_axis"])
    text = json.dumps(to_builtin(identity), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_sha256(path: str | Path) -> str:
    """Return a SHA256 digest for a persisted artifact.

    A single file digests as its bytes. An OME-Zarr position is a directory --
    a description and its chunks -- and digests as every file in it, in path
    order, each one's relative name and then its bytes, so that a changed
    chunk and a moved chunk are both a different position. Opening the
    directory as if it were one file is "permission denied" on Windows and
    "is a directory" on Linux, and it lost the whole detection at the moment
    its record was being written, after Cellpose had already finished.
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


def segment_position(
    image_path,
    state: dict,
    *,
    method: str = "robust",
    threshold=None,
    channels=None,
    z="mid",
    channel_axis=None,
    border_margin_px=None,
    min_area_px=None,
    max_area_px=None,
    cellprob_threshold=None,
    flow_threshold=None,
    niter=None,
    diameter=None,
    segmentation_binning=None,
    gpu: bool = True,
    verbose: int = 0,
    log_prefix: str = "segment",
) -> dict:
    """Load a position, select up to three channels, and find the objects in it.

    ``image_path`` is an OME-Zarr position or an OME-TIFF; both are read
    through the same contract, so nothing below this line knows which it was.
    ``method`` is how the objects are found: ``"robust"`` runs a warm
    Cellpose model on the selected channels (2D or up to 3-channel);
    ``"fast"`` runs the classical watershed of :func:`watershed_masks` on the
    first of them, a second or two a field on the CPU and no model to load.
    Everything around the detector -- the binning, the border and area
    filters, what is returned -- is the same either way. ``image`` preserves
    the selected channels for downstream feature extraction, while
    ``image_2d`` is the primary channel for code paths that require a plane.

    ``channel_axis`` applies only to an array already in memory and is kept
    for callers that hand one over; a file's axes come from its own metadata.
    """
    seg_input, _ = load_channels(image_path, channels, z=z)
    ny, nx = seg_input.shape[:2]
    seg_eval, scale = _downsample_for_segmentation(
        seg_input,
        binning=segmentation_binning,
    )

    if method == "fast":
        # The watershed's sizes are geometric, so they follow the binning
        # the way the picture did; Cellpose is handed the diameter as given.
        plane = seg_eval if seg_eval.ndim == 2 else seg_eval[..., 0]
        masks, fast_params = watershed_masks(
            plane,
            diameter_px=(float(diameter) if diameter is not None else 30.0) * scale,
            threshold=float(threshold) if threshold is not None else 100.0,
        )
        used_gpu, used_device = False, "cpu"
        detector_params = {"method": "fast", "device": used_device, **fast_params}
    elif method == "robust":
        cellpose_channel_axis = -1 if seg_eval.ndim == 3 else None
        eval_kwargs = _cellpose_eval_kwargs(
            cellprob_threshold=cellprob_threshold,
            flow_threshold=flow_threshold,
            niter=niter,
            diameter=diameter,
        )
        model, used_gpu, used_device = _get_cellpose_model(
            state,
            requested_gpu=bool(gpu),
            verbose=verbose,
            log_prefix=log_prefix,
        )
        try:
            masks, flows, styles = model.eval(
                seg_eval,
                channel_axis=cellpose_channel_axis,
                **eval_kwargs,
            )
        except Exception:
            if not gpu:
                raise
            if verbose >= 1:
                print(f"  [{log_prefix}] GPU Cellpose failed; retrying on CPU")
            state.pop("model", None)
            state.pop("_cellpose_model_gpu", None)
            state.pop("_cellpose_model_device", None)
            model, used_gpu, used_device = _get_cellpose_model(
                state,
                requested_gpu=False,
                verbose=verbose,
                log_prefix=log_prefix,
            )
            masks, flows, styles = model.eval(
                seg_eval,
                channel_axis=cellpose_channel_axis,
                **eval_kwargs,
            )
        detector_params = {
            "method": "robust",
            **_cellpose_provenance(model),
            "requested_gpu": bool(gpu),
            "used_gpu": bool(used_gpu),
            "device": used_device,
            "cellprob_threshold": _none_or_float(cellprob_threshold),
            "flow_threshold": _none_or_float(flow_threshold),
            "niter": _none_or_int(niter),
            "diameter": _none_or_float(diameter),
        }
    else:
        raise ValueError(f"unknown detection method {method!r}; have 'robust' and 'fast'")
    if scale != 1.0:
        masks = _resize_nearest(masks, (ny, nx))
    raw_masks = np.asarray(masks, dtype=np.int32)
    raw_n_objects = int(np.count_nonzero(np.bincount(raw_masks.ravel())[1:]))
    # Border first, then size: an object clipped by the tile edge has a
    # smaller area than the object really is, so filtering by size before
    # dropping it would judge it on a measurement the edge invented.
    masks, dropped_labels = filter_masks_by_border(raw_masks, border_margin_px=border_margin_px)
    masks, dropped_by_area = filter_masks_by_area(
        masks,
        min_area_px=min_area_px,
        max_area_px=max_area_px,
    )
    dropped_labels = sorted(set(dropped_labels) | set(dropped_by_area))
    n_objects = int(np.count_nonzero(np.bincount(masks.ravel())[1:]))

    image_2d = seg_input if seg_input.ndim == 2 else seg_input[..., 0]

    if verbose >= 1:
        seg_ny, seg_nx = seg_eval.shape[:2]
        print(
            f"  [{log_prefix}] image={nx}x{ny}, segmentation={seg_nx}x{seg_ny}, objects={n_objects}"
        )

    return {
        "image": seg_input,
        "image_2d": image_2d,
        "raw_masks": raw_masks,
        "masks": masks,
        "n_objects": n_objects,
        "n_raw_objects": raw_n_objects,
        "dropped_labels": dropped_labels,
        "area_filter": {
            "min_area_px": _none_or_int(min_area_px),
            "max_area_px": _none_or_int(max_area_px),
        },
        "border_filter": {"border_margin_px": _none_or_int(border_margin_px)},
        "detector_params": detector_params,
        "segmentation_resize": {
            "binning": _none_or_int(segmentation_binning),
            "scale": float(scale),
            "input_size_px": [int(seg_eval.shape[1]), int(seg_eval.shape[0])],
        },
        "image_size_px": [int(nx), int(ny)],
    }


def watershed_masks(plane, *, diameter_px: float, threshold: float):
    """Nuclei in one plane the way QuPath's cell detection finds them.

    A port of ``WatershedCellDetection`` (Bankhead et al. 2017), label image
    out: the background is estimated by opening by reconstruction and taken
    off; a Gaussian blur and a 3 x 3 Laplacian make the blob response; the
    response's zero crossing is the foreground and its regional maxima seed a
    watershed on it; regions whose mean on the background-subtracted plane
    is under ``threshold`` (counts) are dropped; what is left is filled and
    split again on its distance transform, so touching nuclei part; and
    objects outside the area range go. QuPath's micrometre defaults (8 um
    background radius, 1.5 um sigma, 10 and 400 um^2 areas, for nuclei of
    about 8-10 um) are taken as ratios of the one size the operator gives:
    background radius D, sigma D / 8, areas A / 8 and 5 A with A the disc of
    diameter D. Only scipy and scikit-image, imported here so a Cellpose run
    never pays for them.

    Returns the int32 label image and the parameters as used, in pixels.
    """
    import numpy as np
    from scipy import ndimage
    from skimage import filters, measure, morphology, segmentation

    f = np.asarray(plane, dtype=np.float32)
    d = max(float(diameter_px), 2.0)
    radius = max(int(round(d)), 1)
    sigma = max(d / 8.0, 0.5)
    disc = np.pi * (d / 2.0) ** 2
    min_area = max(int(round(disc / 8.0)), 1)
    max_area = max(int(round(disc * 5.0)), min_area + 1)
    used = {
        "threshold": float(threshold),
        "sigma_px": float(sigma),
        "background_radius_px": int(radius),
        "min_area_px": int(min_area),
        "max_area_px": int(max_area),
        "split_by_shape": True,
    }

    eroded = morphology.erosion(f, morphology.disk(radius, decomposition="sequence"))
    background = morphology.reconstruction(eroded, f, method="dilation")
    subtracted = f - background
    blurred = filters.gaussian(subtracted, sigma=sigma, preserve_range=True)
    response = ndimage.convolve(
        blurred, np.array([[0, -1, 0], [-1, 4, -1], [0, -1, 0]], dtype=np.float32)
    )
    above = response > 0
    seeds = measure.label(morphology.local_maxima(response) & above, connectivity=1)
    regions = segmentation.watershed(-response, seeds, mask=above, connectivity=1)
    labels = np.arange(1, int(regions.max()) + 1)
    if labels.size == 0:
        return np.zeros(f.shape, dtype=np.int32), used
    means = np.asarray(ndimage.mean(subtracted, regions, labels))
    kept = np.isin(regions, labels[means > threshold])
    kept = ndimage.binary_dilation(kept, structure=np.ones((3, 3), dtype=bool)) & above
    kept = ndimage.binary_fill_holes(kept)
    distance = ndimage.distance_transform_edt(kept)
    peaks = measure.label(morphology.h_maxima(distance, 0.5) & kept, connectivity=1)
    split = segmentation.watershed(-distance, peaks, mask=kept, watershed_line=True)
    objects = np.arange(1, int(split.max()) + 1)
    if objects.size:
        means = np.asarray(ndimage.mean(subtracted, split, objects))
        split = np.where(np.isin(split, objects[means >= threshold]), split, 0)
    masks, _ = filter_masks_by_area(
        np.asarray(split, dtype=np.int32), min_area_px=min_area, max_area_px=max_area
    )
    # These are the detector's own labels, so they are numbered 1..n here,
    # once. The filters the operator applies afterwards keep these numbers.
    masks, _, _ = segmentation.relabel_sequential(np.asarray(masks, dtype=np.int32))
    return np.asarray(masks, dtype=np.int32), used


def _get_cellpose_model(state, *, requested_gpu: bool, verbose: int, log_prefix: str):
    """The warm model, on the best device it can get today.

    A model that fell back to the CPU is offered the accelerator again on
    the next call: the card can be full for a moment (a worker just put
    down still holds its memory), and a session that kept the CPU model
    it got in that moment segmented every field ten times slower without
    a word. Once on the card, it stays there.
    """
    if "model" in state:
        cached_gpu = bool(state.get("_cellpose_model_gpu", requested_gpu))
        if requested_gpu and not cached_gpu:
            accelerated = _load_cellpose_model(
                state,
                requested_gpu=True,
                accelerated_only=True,
                verbose=verbose,
                log_prefix=log_prefix,
            )
            if accelerated is not None:
                return accelerated
        return (
            state["model"],
            cached_gpu,
            state.get("_cellpose_model_device", "cuda" if requested_gpu else "cpu"),
        )
    loaded = _load_cellpose_model(
        state,
        requested_gpu=requested_gpu,
        accelerated_only=False,
        verbose=verbose,
        log_prefix=log_prefix,
    )
    if loaded is None:
        raise RuntimeError(
            "Could not initialize CellposeModel on any device: " + state.pop("_cellpose_errors", "")
        )
    return loaded


def _load_cellpose_model(state, *, requested_gpu, accelerated_only, verbose, log_prefix):
    """Try the devices in order; the first that loads is kept in *state*.

    ``None`` when none of the tried devices would load; with
    ``accelerated_only`` the CPU is not tried, because a CPU model is what
    the caller already has.
    """
    from cellpose import models

    errors = []
    for device_name, is_accelerated, kwargs in _cellpose_device_candidates(requested_gpu):
        if accelerated_only and not is_accelerated:
            continue
        try:
            if verbose >= 2:
                print(f"  [{log_prefix}] cold start: loading CellposeModel({device_name})")
            state["model"] = _instantiate_cellpose_model(models, kwargs)
            state["_cellpose_model_gpu"] = is_accelerated
            state["_cellpose_model_device"] = device_name
            return state["model"], is_accelerated, device_name
        except Exception as exc:
            errors.append(f"{device_name}: {exc}")
            if verbose >= 1 and device_name != "cpu":
                print(f"  [{log_prefix}] Cellpose {device_name} unavailable; trying next device")

    state["_cellpose_errors"] = "; ".join(errors)
    return None


def _cellpose_device_candidates(prefer_accelerator: bool):
    if not prefer_accelerator:
        return [("cpu", False, {"gpu": False})]

    candidates = []
    try:
        import torch
    except Exception:
        torch = None

    if torch is not None and torch.cuda.is_available():
        candidates.append(("cuda", True, {"gpu": True, "device": torch.device("cuda")}))
    if torch is not None and hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        candidates.append(("mps", True, {"gpu": True, "device": torch.device("mps")}))
    candidates.append(("cpu", False, {"gpu": False}))
    return candidates


def _cellpose_provenance(model) -> dict:
    """Which Cellpose produced the masks: its version, its network, and the
    torch it ran on. The masks change with all three, so a table without
    them cannot be reproduced; they go into ``detector_params`` beside the
    thresholds, where the checkpoint keeps them."""
    out = {}
    try:
        import cellpose

        out["cellpose_version"] = str(
            getattr(cellpose, "version", None) or getattr(cellpose, "__version__", "unknown")
        )
    except Exception:  # noqa: BLE001 - provenance never fails a detection
        out["cellpose_version"] = "unknown"
    network = getattr(model, "pretrained_model", None)
    if network is not None and not isinstance(network, str):
        network = str(network[0]) if len(network) else None
    out["cellpose_model"] = Path(network).name if network else "default"
    try:
        import torch

        out["torch_version"] = str(torch.__version__)
    except Exception:  # noqa: BLE001
        out["torch_version"] = None
    return out


def _instantiate_cellpose_model(models, kwargs):
    try:
        return models.CellposeModel(**kwargs)
    except TypeError:
        if "device" not in kwargs:
            raise
        return models.CellposeModel(gpu=bool(kwargs.get("gpu", False)))


def select_channels(image, channels=None, channel_axis=None):
    """Return up to three channels for Cellpose as ``(H, W)`` or ``(H, W, k)``.

    ``channels`` chooses which channels to keep; ``None`` uses the first up to
    three. A single selected channel is returned as a 2D plane.

    ``channel_axis`` declares the orientation of a 3D input explicitly: ``0``
    for channel-first ``(C, H, W)`` or ``-1``/``2`` for channel-last
    ``(H, W, C)``. When it is ``None`` the orientation is inferred by treating
    the smaller end axis as the channel axis. Inference is refused when the two
    end axes are equal, because channel-first and channel-last are then
    indistinguishable; pass ``channel_axis`` in that case.
    """
    if image.ndim == 2:
        if channels is not None:
            indices = _channel_indices(channels)
            if indices not in ([], [0]):
                raise ValueError("channels for a 2D image must be None or [0].")
        return image
    if image.ndim != 3:
        raise ValueError(
            f"Cannot select channels from image with shape {image.shape}. "
            f"Expected 2D (H, W) or 2D plus channels: (C, H, W) / (H, W, C)."
        )

    stack = _to_channel_last(image, channel_axis)
    n_channels = stack.shape[-1]
    if channels is None:
        indices = list(range(min(n_channels, 3)))
    else:
        indices = _channel_indices(channels)
        if not indices:
            raise ValueError("channels must contain at least one channel.")
        if len(indices) > 3:
            raise ValueError("Cellpose accepts at most 3 channels.")
        if any(c < 0 or c >= n_channels for c in indices):
            raise ValueError(f"channels {indices} out of range for {n_channels} channels.")

    selected = stack[..., indices]
    if selected.shape[-1] == 1:
        return selected[..., 0]
    return selected


def _to_channel_last(image, channel_axis):
    """Normalize a 3D array to channel-last ``(H, W, C)``.

    With ``channel_axis`` given, the orientation is taken as declared. With
    ``channel_axis=None`` it is inferred from axis sizes: the smaller end axis
    is the channel axis. Equal end axes are ambiguous and raise ``ValueError``.
    """
    if channel_axis is not None:
        if isinstance(channel_axis, (bool, np.bool_)):
            raise ValueError(f"channel_axis must be 0, 2, -1, or None; got {channel_axis}.")
        try:
            channel_axis = operator.index(channel_axis)
        except TypeError as exc:
            raise ValueError(
                f"channel_axis must be 0, 2, -1, or None; got {channel_axis}."
            ) from exc
        if channel_axis in (-1, 2):
            return image
        if channel_axis == 0:
            return np.moveaxis(image, 0, -1)
        raise ValueError(f"channel_axis must be 0, 2, -1, or None; got {channel_axis}.")

    first, last = image.shape[0], image.shape[-1]
    if first == last:
        raise ValueError(
            f"Cannot infer channel axis for image with shape {image.shape}: "
            f"the first and last axes are equal, so channel-first (C, H, W) "
            f"and channel-last (H, W, C) are indistinguishable. Pass "
            f"channel_axis explicitly (0 for channel-first, -1 for "
            f"channel-last)."
        )
    # Channels are fewer than spatial pixels, so the smaller end is the
    # channel axis; channel-first is normalized to channel-last.
    if first < last:
        return np.moveaxis(image, 0, -1)
    return image


def _channel_indices(channels) -> list[int]:
    if isinstance(channels, (int, np.integer)):
        return [int(channels)]
    return [int(c) for c in channels]


def filter_masks_by_area(masks, *, min_area_px=None, max_area_px=None):
    min_area_px = _none_or_int(min_area_px)
    max_area_px = _none_or_int(max_area_px)
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
    exists -- which is why a margin is preferred here over reconciling
    duplicates afterwards.

    The margin is the width of the rejected band at each edge, in pixels of
    the mask. ``None`` or ``0`` rejects nothing. Set it to about half the
    overlap and an object counted twice becomes an object counted once; set
    it wider and objects are lost from the seam instead.

    Returns ``(masks, dropped_labels)`` with the survivors relabelled from 1,
    matching :func:`filter_masks_by_area`.
    """
    border_margin_px = _none_or_int(border_margin_px)
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
    """Keep only *keep*, at their own label numbers; return (masks, dropped).

    The labels are the detector's, and they are kept as they are: an object
    that survives a filter keeps the number it had, so its ``object_id`` and
    the crops written under it stay the same when the border margin or the
    size bounds are retuned from the saved raw masks. Renumbering would
    give the same cell a different name every time the filter changed, and
    ``dropped_labels`` would name labels that no longer mean anything.
    """
    present = np.flatnonzero(np.bincount(masks.ravel()))
    keep_set = set(int(k) for k in keep)
    dropped = sorted(int(label) for label in present if label and int(label) not in keep_set)
    kept = np.where(np.isin(masks, list(keep_set)), masks, 0)
    return kept.astype(np.int32, copy=False), dropped


_filter_masks_by_area = filter_masks_by_area


def _downsample_for_segmentation(image, *, binning=None):
    binning = _none_or_int(binning)
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


def _resize_area(image, shape):
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


def _resize_nearest(image, shape):
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


def _cellpose_eval_kwargs(
    *,
    cellprob_threshold=None,
    flow_threshold=None,
    niter=None,
    diameter=None,
):
    kwargs = {}
    if cellprob_threshold is not None:
        kwargs["cellprob_threshold"] = float(cellprob_threshold)
    if flow_threshold is not None:
        kwargs["flow_threshold"] = float(flow_threshold)
    if niter is not None:
        kwargs["niter"] = int(niter)
    if diameter is not None:
        kwargs["diameter"] = float(diameter)
    return kwargs


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

REQUIRED_GEOMETRY_FIELDS = (
    "tile_id",
    "tile_stage_xy_um",
    "source_pixel_size_um",
    "source_image_size_px",
    "image_to_stage",
)

REQUIRED_TARGET_FIELDS = (
    "target_id",
    "tile_id",
    "object_label",
    "score",
    "source_feature",
    "centroid_row_px",
    "centroid_col_px",
    "bbox_min_row_px",
    "bbox_min_col_px",
    "bbox_max_row_px",
    "bbox_max_col_px",
    "stage_x_um",
    "stage_y_um",
)


def to_builtin(obj: Any) -> Any:
    """Return a JSON-native copy of ``obj``.

    Numpy arrays/scalars are converted via ``tolist``/``item`` without
    importing numpy. Tuples become lists, and non-finite floats become
    ``None`` so ``json.dump(..., allow_nan=False)`` remains valid.
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
        # Only true scalars take the item() door: a ONE-element numpy array
        # answers item() just as happily, and taking it collapsed every
        # column of a single-object field to a bare number -- len() then
        # died on an int at the top of the table step.
        try:
            return to_builtin(obj.item())
        except (TypeError, ValueError):
            pass
    if hasattr(obj, "tolist"):
        return to_builtin(obj.tolist())
    raise TypeError(f"Value of type {type(obj).__name__} is not JSON-native.")


def validate_tile_detection(tile: Mapping[str, Any]) -> dict:
    """Validate and return a JSON-native per-tile detection output."""
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


def validate_targets(result: Mapping[str, Any]) -> dict:
    """Validate and return JSON-native target discovery output."""
    result = _json_checked(to_builtin(result), "targets result")
    if not isinstance(result, dict):
        raise ValueError("targets result must be a dict.")
    targets = result.get("targets")
    if not isinstance(targets, list):
        raise ValueError("targets must be a list.")

    for idx, target in enumerate(targets):
        if not isinstance(target, dict):
            raise ValueError(f"targets[{idx}] must be a dict.")
        for name in REQUIRED_TARGET_FIELDS:
            if name not in target:
                raise ValueError(f"targets[{idx}] missing required field {name!r}.")
        if not isinstance(target["target_id"], list):
            raise ValueError(f"targets[{idx}].target_id must be a list.")
        if not isinstance(target["tile_id"], list):
            raise ValueError(f"targets[{idx}].tile_id must be a list.")
        for name in REQUIRED_TARGET_FIELDS:
            if name in {"target_id", "tile_id"}:
                continue
            if isinstance(target[name], (dict, list)):
                raise ValueError(f"targets[{idx}].{name} must be a scalar.")

    return result


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
