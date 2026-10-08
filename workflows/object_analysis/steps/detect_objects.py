"""detect_objects -- find the objects in one image tile with Cellpose.

Takes ``input["image_path"]``, an OME-Zarr position or an OME-TIFF, and
the tile's geometry (see the workflow README, "Input"). Up to three
channels are read from it and handed to Cellpose, which answers with a
label image: zero for background, a number of its own for every object.
Then the objects within ``border_margin_px`` of the edge are dropped,
because the neighbouring tile holds them too, and so are those outside the
size bounds. The masks and a record of how they were made are filed beside
the data (``parts/checkpoint.py``).

Every setting has its default in the recipe, and a submit may override any
of them for one image by naming the same key in its input. ``method`` may
be ``"fast"`` to run the watershed instead of Cellpose; the step file
``detect_objects_fast`` does that in the environment without torch.

Publishes under ``pipeline_data["detect_objects"]``::

    image            the channels that were read, for the feature step
    masks            the label image after the filters
    n_objects        how many objects survived the filters
    n_raw_objects    how many the detector found
    dropped_labels   the labels the filters removed
    detector_params  which detector ran, on which device, with which tuning
    segmentation_params, segmentation_params_hash
                     what decided the masks, and its one-line identity
    artifacts        the files the checkpoint wrote, when it wrote any

With ``persist_only: true`` only the checkpoint is wanted: the image and
masks are left out of the result, and the pipeline stops here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from object_analysis.parts.cellpose_model import (  # noqa: E402
    cellpose_detector_params,
    cellpose_eval_kwargs,
    forget_cellpose_model,
    get_cellpose_model,
)
from object_analysis.parts.checkpoint import write_detection_checkpoint  # noqa: E402
from object_analysis.parts.masks import (  # noqa: E402
    downsample_for_segmentation,
    filter_masks_by_area,
    filter_masks_by_border,
    resize_nearest,
)
from object_analysis.parts.settings import (  # noqa: E402
    area_filter_params,
    border_filter_params,
    none_or_int,
    segmentation_params,
    segmentation_params_hash,
)
from object_analysis.parts.watershed import watershed_masks  # noqa: E402
from shared.image_io import load_channels, load_plane  # noqa: E402

METADATA = {
    "description": "Detect objects in an image tile with Cellpose",
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
        z=seg_params["z_selection"],
        method=seg_params["method"],
        threshold=seg_params["threshold"],
        channels=seg_params["channels"],
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
    artifacts = write_detection_checkpoint(detection, raw_masks, inp, params)
    if artifacts:
        detection["artifacts"] = artifacts

    if bool(params.get("persist_only", False)):
        for heavy in ("image", "masks"):
            detection.pop(heavy, None)
        pipeline_data["detect_objects"] = detection
        return pipeline_data

    # The feature step measures intensity in every colour it is given, so
    # the other channels of the frame join the detection image here,
    # channel-last. Segmentation itself stays on the channels it was asked
    # for. A frame stored one file per plane names the other files
    # (``extra_channel_paths``); one stored as a single position names the
    # channel indices to read from it (``extra_channel_indices``).
    image = detection["image"]
    extra_paths = inp.get("extra_channel_paths") or []
    extra_channels = inp.get("extra_channel_indices") or []
    if extra_paths:
        from tifffile import imread

        planes = [np.asarray(image)]
        planes += [np.asarray(imread(str(path))) for path in extra_paths]
        image = np.stack(planes, axis=-1)
    elif extra_channels:
        planes = [np.asarray(image)]
        planes += [
            np.asarray(
                load_plane(inp["image_path"], c=int(channel), z=seg_params["z_selection"])[0]
            )
            for channel in extra_channels
        ]
        image = np.stack(planes, axis=-1)
    detection["image"] = image
    pipeline_data["detect_objects"] = detection
    return pipeline_data


def segment_position(
    image_path,
    state: dict,
    *,
    method: str = "robust",
    threshold=None,
    channels=None,
    z="mid",
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
    Cellpose model on the selected channels (one, or up to three stacked
    channel-last); ``"fast"`` runs the watershed of ``parts/watershed.py`` on
    the first of them, a second or two a field on the processor and no model
    to load. Everything around the detector, the binning, the border and
    area filters, what is returned, is the same either way.

    Returns the detection: ``image`` (the channels read, as the feature step
    needs them), ``raw_masks`` and ``masks`` (before and after the filters),
    the counts, ``dropped_labels``, the filters as applied,
    ``detector_params`` and ``segmentation_resize``.
    """
    seg_input, _ = load_channels(image_path, channels, z=z)
    ny, nx = seg_input.shape[:2]
    seg_eval, scale = downsample_for_segmentation(seg_input, binning=segmentation_binning)

    if method == "fast":
        # The watershed's sizes are geometric, so they follow the binning
        # the way the picture did; Cellpose is handed the diameter as given.
        plane = seg_eval if seg_eval.ndim == 2 else seg_eval[..., 0]
        masks, fast_params = watershed_masks(
            plane,
            diameter_px=(float(diameter) if diameter is not None else 30.0) * scale,
            threshold=float(threshold) if threshold is not None else 100.0,
        )
        detector_params = {"method": "fast", "device": "cpu", **fast_params}
    elif method == "robust":
        masks, detector_params = _cellpose_masks(
            seg_eval,
            state,
            gpu=gpu,
            verbose=verbose,
            log_prefix=log_prefix,
            cellprob_threshold=cellprob_threshold,
            flow_threshold=flow_threshold,
            niter=niter,
            diameter=diameter,
        )
    else:
        raise ValueError(f"unknown detection method {method!r}; have 'robust' and 'fast'")

    if scale != 1.0:
        masks = resize_nearest(masks, (ny, nx))
    raw_masks = np.asarray(masks, dtype=np.int32)
    raw_n_objects = int(np.count_nonzero(np.bincount(raw_masks.ravel())[1:]))
    # Border first, then size: an object clipped by the tile edge has a
    # smaller area than the object really is, so filtering by size before
    # dropping it would judge it on a measurement the edge invented.
    masks, dropped_labels = filter_masks_by_border(raw_masks, border_margin_px=border_margin_px)
    masks, dropped_by_area = filter_masks_by_area(
        masks, min_area_px=min_area_px, max_area_px=max_area_px
    )
    dropped_labels = sorted(set(dropped_labels) | set(dropped_by_area))
    n_objects = int(np.count_nonzero(np.bincount(masks.ravel())[1:]))

    if verbose >= 1:
        seg_ny, seg_nx = seg_eval.shape[:2]
        print(
            f"  [{log_prefix}] image={nx}x{ny}, segmentation={seg_nx}x{seg_ny}, objects={n_objects}"
        )

    return {
        "image": seg_input,
        "raw_masks": raw_masks,
        "masks": masks,
        "n_objects": n_objects,
        "n_raw_objects": raw_n_objects,
        "dropped_labels": dropped_labels,
        "area_filter": {
            "min_area_px": none_or_int(min_area_px),
            "max_area_px": none_or_int(max_area_px),
        },
        "border_filter": {"border_margin_px": none_or_int(border_margin_px)},
        "detector_params": detector_params,
        "segmentation_resize": {
            "binning": none_or_int(segmentation_binning),
            "scale": float(scale),
            "input_size_px": [int(seg_eval.shape[1]), int(seg_eval.shape[0])],
        },
        "image_size_px": [int(nx), int(ny)],
    }


def _cellpose_masks(seg_eval, state, *, gpu, verbose, log_prefix, **tuning):
    """Run the warm Cellpose model; on a GPU failure, try once more on the processor."""
    channel_axis = -1 if seg_eval.ndim == 3 else None
    eval_kwargs = cellpose_eval_kwargs(**tuning)
    model, used_gpu, device = get_cellpose_model(
        state, requested_gpu=bool(gpu), verbose=verbose, log_prefix=log_prefix
    )
    try:
        masks, _flows, _styles = model.eval(seg_eval, channel_axis=channel_axis, **eval_kwargs)
    except Exception:
        if not gpu:
            raise
        if verbose >= 1:
            print(f"  [{log_prefix}] GPU Cellpose failed; retrying on CPU")
        forget_cellpose_model(state)
        model, used_gpu, device = get_cellpose_model(
            state, requested_gpu=False, verbose=verbose, log_prefix=log_prefix
        )
        masks, _flows, _styles = model.eval(seg_eval, channel_axis=channel_axis, **eval_kwargs)
    params = cellpose_detector_params(model, gpu=gpu, used_gpu=used_gpu, device=device, **tuning)
    return masks, params
