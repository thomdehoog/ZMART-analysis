"""measure_registration -- how far apart two objectives look and focus.

A target found with a low-power lens is imaged again with a high-power one,
and the two lenses do not look at exactly the same place or focus at exactly
the same height. This step measures both from pictures taken with the stage
standing still.

- Where they look: both pictures are brought to the coarser pixel size and
  laid over each other; the shift between them is how far the target lens
  looks from the reference lens.
- Where they focus: each lens's focus stack is scored for sharpness (Brenner,
  from ``score_focus``) and the sharp height read off; the difference is how
  far apart they focus.

Takes ``reference`` and ``target`` from ``read_images``: each an ``image`` taken
at one stage position, already corrected for the rig's orientation, its
``pixel_um``, and optionally a ``stack`` with ``z_um``.

Publishes under ``pipeline_data["measure_registration"]``::

    translation_um   x, y, z: where the target lens looks and focuses
                     relative to the reference lens (z None without stacks)
    pixel_um         both pixel sizes, and the one they were compared at
    focus            the sharp height under each lens, or None
    registration     the shift in pixels, and how well the pictures agree
    accepted, why    whether the answer can be trusted, and if not, why not
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from scipy.ndimage import zoom
from skimage.registration import phase_cross_correlation

sys.path.insert(0, str(Path(__file__).resolve().parent))
from score_focus import brenner  # noqa: E402

METADATA = {
    "description": "Where and at what height a target objective looks relative to a reference one",
    "version": "1.0",
    "max_workers": 1,
    "environment": "ZMART--alignment--main",
}

#: How well the two pictures must agree where they overlap, once shifted, for
#: the shift to be believed: 1 is identical, 0 unrelated. Two lenses resolve
#: differently, so they agree strongly but never perfectly.
AGREEMENT_MIN = 0.5


def run(pipeline_data: dict, state: dict, **params) -> dict:

    images = pipeline_data["read_images"]

    upsample = int(params.get("upsample", 20))

    agreement_min = float(params.get("agreement_min", AGREEMENT_MIN))

    reference, target = images["reference"], images["target"]

    ref_um, tgt_um = float(reference["pixel_um"]), float(target["pixel_um"])

    # The finer picture goes down to the coarser scale: more pixels add nothing to a correlation
    scale_um = max(ref_um, tgt_um)

    ref, tgt = _same_scale(reference["image"], ref_um, target["image"], tgt_um, scale_um)

    # Where the target's features sit relative to the reference's, in pixels
    shift, error, _ = phase_cross_correlation(ref, tgt, upsample_factor=upsample)

    dcol_px, drow_px = float(-shift[1]), float(-shift[0])

    agreement = _overlap_agreement(ref, tgt, dcol_px, drow_px)

    # Features further right under the target lens: the target lens looks further left
    dx_um, dy_um = -dcol_px * scale_um, -drow_px * scale_um

    focus = {side: _sharp_height(images[side]) for side in ("reference", "target")}

    dz_um, unbracketed = _focus_difference(focus)

    accepted = agreement >= agreement_min and not unbracketed

    answer = {
        "translation_um": {"x": dx_um, "y": dy_um, "z": dz_um},
        "pixel_um": {"reference": ref_um, "target": tgt_um, "overlay": scale_um},
        "focus": focus,
        "registration": {
            "dcol_px": dcol_px,
            "drow_px": drow_px,
            "agreement": agreement,
            "error": float(error),
        },
        "accepted": bool(accepted),
        "why": _why(agreement, agreement_min, unbracketed),
        "settings": {"channel": images["channel"], "upsample": upsample},
    }

    # Only the answer goes back: the pictures stay where they came from
    return {"measure_registration": answer}


def _same_scale(
    ref, ref_um: float, tgt, tgt_um: float, scale_um: float
) -> tuple[np.ndarray, np.ndarray]:
    """Both pictures at one pixel size, cut to the same size around their middles."""

    pictures = []

    for image, pixel_um in ((ref, ref_um), (tgt, tgt_um)):
        image = image.astype(np.float64)

        factor = pixel_um / scale_um

        pictures.append(image if abs(factor - 1.0) < 1e-9 else zoom(image, factor, order=1))

    rows = min(picture.shape[0] for picture in pictures)

    cols = min(picture.shape[1] for picture in pictures)

    return tuple(_middle(picture, rows, cols) for picture in pictures)


def _middle(image: np.ndarray, rows: int, cols: int) -> np.ndarray:
    """The middle ``rows`` by ``cols`` of a picture."""

    top = (image.shape[0] - rows) // 2

    left = (image.shape[1] - cols) // 2

    return image[top : top + rows, left : left + cols]


def _overlap_agreement(reference: np.ndarray, moved: np.ndarray, dcol: float, drow: float) -> float:
    """The correlation of the two pictures where they overlap, once shifted: 1 is identical.

    What sits at (r, c) in the moved picture sat at (r - drow, c - dcol) in
    the reference.
    """

    dc, dr = int(round(dcol)), int(round(drow))

    rows, cols = reference.shape

    a = reference[max(0, -dr) : min(rows, rows - dr), max(0, -dc) : min(cols, cols - dc)].ravel()

    b = moved[max(0, dr) : min(rows, rows + dr), max(0, dc) : min(cols, cols + dc)].ravel()

    if a.size < 16 or a.std() == 0 or b.std() == 0:
        return 0.0

    return float(np.corrcoef(a, b)[0, 1])


def _sharp_height(lens: dict) -> dict | None:
    """The sharp height of one lens's focus stack, refined between planes; None without a stack.

    A peak on the first or last plane is not bracketed: the sharpness never
    fell away on that side, so the true focus may lie beyond the stack.
    """

    if lens.get("stack") is None or not len(lens["stack"]):
        return None

    z = np.asarray(lens["z_um"], dtype=float)

    if len(lens["stack"]) != len(z):
        raise ValueError(f"{len(lens['stack'])} planes but {len(z)} heights")

    scores = np.array([brenner(plane.astype(np.float64)) for plane in lens["stack"]])

    best = int(np.argmax(scores))

    peak_z = float(z[best])

    bracketed = 0 < best < len(scores) - 1

    if bracketed:
        a, b, c = scores[best - 1], scores[best], scores[best + 1]

        curvature = a - 2 * b + c

        if curvature < 0:
            offset = 0.5 * (a - c) / curvature

            step = float(z[best + 1] - z[best - 1]) / 2.0

            peak_z = float(z[best] + offset * step)

    return {
        "peak_z_um": peak_z,
        "peak_index": best,
        "bracketed": bool(bracketed),
        "scores": scores.tolist(),
        "z_um": z.tolist(),
    }


def _focus_difference(focus: dict) -> tuple[float | None, list[str]]:
    """How much higher the target lens focuses, and which stacks did not bracket their peak.

    No difference is reported when either peak sits on the end of its stack.
    """

    unbracketed = [
        side for side, found in focus.items() if found is not None and not found["bracketed"]
    ]

    if unbracketed or not (focus["reference"] and focus["target"]):
        return None, unbracketed

    return focus["target"]["peak_z_um"] - focus["reference"]["peak_z_um"], unbracketed


def _why(agreement: float, agreement_min: float, unbracketed: list[str]) -> str | None:
    """What to do when the answer is not to be trusted, or None."""

    if unbracketed:
        return (
            f"the focus peak sits at the end of the {' and '.join(unbracketed)} stack, so the "
            "sharpest plane was not reached; refocus under that lens and measure it again"
        )

    if agreement < agreement_min:
        return (
            f"the two pictures agree only {agreement:.2f} where they overlap (less than "
            f"{agreement_min}); check that both were taken at the same stage position and in focus"
        )

    return None
