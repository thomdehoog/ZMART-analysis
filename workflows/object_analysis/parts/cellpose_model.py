"""Loading Cellpose once per worker, on the best device the computer has.

Loading the model takes seconds; using it takes a fraction of that. The
worker keeps the loaded model in the step's ``state``, so every field after
the first pays only for the segmentation. Which device it runs on, and which
version of Cellpose and of its network produced the masks, is recorded with
every detection, because the masks change with all three.
"""

from __future__ import annotations

from pathlib import Path

from .settings import none_or_float, none_or_int


def get_cellpose_model(state, *, requested_gpu: bool, verbose: int, log_prefix: str):
    """The warm model, on the best device it can get today.

    Returns ``(model, used_gpu, device_name)``. A model that fell back to the
    processor is offered the graphics card again on the next call: the card
    can be full for a moment (a worker just put down still holds its
    memory), and a session that kept the processor model it got in that
    moment segmented every field ten times slower without a word. Once on
    the card, it stays there.
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


def forget_cellpose_model(state) -> None:
    """Drop the loaded model from ``state``, so the next call loads afresh."""
    state.pop("model", None)
    state.pop("_cellpose_model_gpu", None)
    state.pop("_cellpose_model_device", None)


def _load_cellpose_model(state, *, requested_gpu, accelerated_only, verbose, log_prefix):
    """Try the devices in order; the first that loads is kept in *state*.

    ``None`` when none of the tried devices would load; with
    ``accelerated_only`` the processor is not tried, because a processor
    model is what the caller already has.
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
    """The devices to try, best first: CUDA, then Apple's MPS, then the processor."""
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


def _instantiate_cellpose_model(models, kwargs):
    try:
        return models.CellposeModel(**kwargs)
    except TypeError:
        if "device" not in kwargs:
            raise
        return models.CellposeModel(gpu=bool(kwargs.get("gpu", False)))


def cellpose_provenance(model) -> dict:
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


def cellpose_eval_kwargs(
    *, cellprob_threshold=None, flow_threshold=None, niter=None, diameter=None
):
    """The tuning Cellpose's ``eval`` is handed: only what was set, typed."""
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


def cellpose_detector_params(model, *, gpu, used_gpu, device, **tuning) -> dict:
    """What goes into ``detector_params`` for a Cellpose detection."""
    return {
        "method": "robust",
        **cellpose_provenance(model),
        "requested_gpu": bool(gpu),
        "used_gpu": bool(used_gpu),
        "device": device,
        "cellprob_threshold": none_or_float(tuning.get("cellprob_threshold")),
        "flow_threshold": none_or_float(tuning.get("flow_threshold")),
        "niter": none_or_int(tuning.get("niter")),
        "diameter": none_or_float(tuning.get("diameter")),
    }
