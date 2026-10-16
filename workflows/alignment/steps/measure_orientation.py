"""measure_orientation -- which way the picture is turned relative to the stage.

A camera is often mounted a quarter or half turn away from the stage's X and
Y, and some settings mirror the picture as well. This step finds out from
three pictures of one field: at home, after the stage moved a known distance
along +X, and after the same move along +Y. Where the features went tells
how the picture is turned; how far they went tells how much specimen one
pixel covers.

The answer is one of eight lossless layouts: a whole quarter turn, with or
without a left-right mirror. A rig turned by part of a turn is reported as
not accepted rather than corrected around.

Takes ``home``, ``plus_x`` and ``plus_y`` from ``read_images``, raw as the
camera recorded them, and ``input["stage_move_um"]``.

Sign convention: on a rig with nothing turned, a stage move of +X moves the
features to lower column numbers. The measured matrix holds how features move
per micrometre of stage travel, and the correction is ``-inv`` of it.

Publishes under ``pipeline_data["measure_orientation"]``::

    orientation     the document a driver adopts: rotation_deg, reflection,
                    sign_convention, measured, schema_version
    accepted, why   whether the fit is close enough to a whole quarter turn
    residual        how far the fit sat from the nearest of the eight
    image_to_stage  the 2x2 matrix of the chosen layout
    pixel_um        micrometres per pixel along x, y, and their mean
    registrations   the feature shifts, in pixels, for the record
"""

from __future__ import annotations

import numpy as np
from skimage.feature import match_template
from skimage.registration import phase_cross_correlation

METADATA = {
    "description": "Which of the eight lossless orientations lines the picture up with the stage",
    "version": "1.0",
    "max_workers": 1,
    "environment": "ZMART--alignment--main",
}

SCHEMA_VERSION = 3

#: Where each layout sends one column and one row of the picture, in stage
#: axes: (rotation clockwise in degrees, mirrored) -> 2x2 matrix. The mirror
#: comes first and the turn second, so every layout has one name.
STAGE_FROM_ORIENTATION = {
    (0, False): ((1, 0), (0, 1)),
    (90, False): ((0, -1), (1, 0)),
    (180, False): ((-1, 0), (0, -1)),
    (270, False): ((0, 1), (-1, 0)),
    (0, True): ((-1, 0), (0, 1)),
    (90, True): ((0, -1), (-1, 0)),
    (180, True): ((1, 0), (0, -1)),
    (270, True): ((0, 1), (1, 0)),
}

#: How far the fitted matrix may sit from the nearest layout and still be
#: believed. A rig turned by 10 degrees gives about 0.25.
RESIDUAL_MAX = 0.25


def run(pipeline_data: dict, state: dict, **params) -> dict:

    images = pipeline_data["read_images"]

    residual_max = float(params.get("residual_max", RESIDUAL_MAX))

    upsample = int(params.get("upsample", 20))

    stage_move_um = float(pipeline_data["input"]["stage_move_um"])

    home, plus_x, plus_y = (
        images[name].astype(np.float64) for name in ("home", "plus_x", "plus_y")
    )

    _check(home, plus_x, plus_y, stage_move_um)

    # How far the features moved after each stage move, in pixels
    shift_x = _feature_shift(home, plus_x, upsample)

    shift_y = _feature_shift(home, plus_y, upsample)

    # Pixels per micrometre of stage travel, one column per stage axis
    moved = (
        np.array(
            [
                [shift_x["dcol_px"], shift_y["dcol_px"]],
                [shift_x["drow_px"], shift_y["drow_px"]],
            ]
        )
        / stage_move_um
    )

    pixel_um = _pixel_size(moved)

    # Direction alone, then the correction the sign convention asks for
    (rotation_deg, reflection), residual = _nearest_layout(_correction(moved * pixel_um["mean"]))

    accepted = residual <= residual_max

    answer = {
        "orientation": _orientation_document(rotation_deg, reflection),
        "accepted": bool(accepted),
        "why": _why(accepted, residual, residual_max, pixel_um),
        "residual": float(residual),
        "residual_max": residual_max,
        "image_to_stage": [list(row) for row in STAGE_FROM_ORIENTATION[(rotation_deg, reflection)]],
        "pixel_um": pixel_um,
        "stage_move_um": stage_move_um,
        "registrations": {"stage_plus_x": shift_x, "stage_plus_y": shift_y},
        "settings": {"channel": images["channel"], "upsample": upsample},
    }

    # Only the answer goes back: the pictures stay where they came from
    return {"measure_orientation": answer}


def _check(home, plus_x, plus_y, stage_move_um: float) -> None:
    """Refuse a move of zero, and pictures of different sizes."""

    if stage_move_um <= 0:
        raise ValueError(f"stage_move_um must be > 0, got {stage_move_um}")

    for name, image in (("plus_x", plus_x), ("plus_y", plus_y)):
        if image.shape != home.shape:
            raise ValueError(f"{name} is {image.shape}, home is {home.shape}; they must match")


def _feature_shift(reference: np.ndarray, moved: np.ndarray, upsample: int) -> dict:
    """How far the features moved from the first picture to the second, in pixels.

    Phase correlation finds the shift to a fraction of a pixel. It can lock
    onto the wrong answer on a field with little texture, so the shift is
    checked by laying the pictures over each other; when they then agree
    poorly, a template match decides instead, and ``method`` says which.
    """

    shift, error, _ = phase_cross_correlation(reference, moved, upsample_factor=upsample)

    dcol, drow = float(-shift[1]), float(-shift[0])

    agreement = _overlap_agreement(reference, moved, dcol, drow)

    method = "phase"

    if agreement < 0.5:
        dcol, drow = _template_shift(reference, moved)

        agreement = _overlap_agreement(reference, moved, dcol, drow)

        method = "template"

    return {
        "dcol_px": dcol,
        "drow_px": drow,
        "agreement": agreement,
        "method": method,
        "error": float(error),
    }


def _template_shift(reference: np.ndarray, moved: np.ndarray) -> tuple[float, float]:
    """The whole-pixel shift at which the middle of the moved picture fits the reference best."""

    rows, cols = moved.shape

    template = moved[rows // 4 : 3 * rows // 4, cols // 4 : 3 * cols // 4]

    score = match_template(reference, template)

    row, col = np.unravel_index(int(np.argmax(score)), score.shape)

    return float(cols // 4 - col), float(rows // 4 - row)


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


def _pixel_size(moved: np.ndarray) -> dict:
    """Micrometres per pixel along each stage axis, and their mean."""

    lengths = np.linalg.norm(moved, axis=0)

    if not np.all(np.isfinite(lengths)) or np.any(lengths <= 0):
        raise ValueError(
            "the features did not move; the stage move was too small or the field is flat"
        )

    x_um, y_um = float(1.0 / lengths[0]), float(1.0 / lengths[1])

    return {"x": x_um, "y": y_um, "mean": float(np.mean([x_um, y_um]))}


def _correction(unit: np.ndarray) -> np.ndarray:
    """The matrix that lines the picture up with the stage: ``-inv`` of how features move."""

    try:
        return -np.linalg.inv(unit)
    except np.linalg.LinAlgError as exc:
        raise ValueError(
            f"the two moves did not span the picture ({exc}); the shifts are collinear"
        ) from exc


def _nearest_layout(fitted: np.ndarray) -> tuple[tuple[int, bool], float]:
    """The layout closest to a fitted matrix, and how far it sat."""

    distances = {
        key: float(np.linalg.norm(fitted - np.asarray(matrix, dtype=float)))
        for key, matrix in STAGE_FROM_ORIENTATION.items()
    }

    best = min(distances, key=distances.get)

    return best, distances[best]


def _orientation_document(rotation_deg: int, reflection: bool) -> dict:
    """The document a driver adopts, in the shape of its orientation.json."""

    rows = STAGE_FROM_ORIENTATION[(rotation_deg, reflection)]

    return {
        "schema_version": SCHEMA_VERSION,
        "measured": True,
        "rotation_deg": int(rotation_deg),
        "reflection": bool(reflection),
        "sign_convention": {
            "stage_x_from_image": _signed_axis(rows[0]),
            "stage_y_from_image": _signed_axis(rows[1]),
        },
    }


def _signed_axis(row) -> str:
    """The image axis a stage axis comes from, with its sign: ``+X``, ``-Y``, ..."""

    index = 0 if row[0] else 1

    return f"{'+' if row[index] > 0 else '-'}{('X', 'Y')[index]}"


def _why(accepted: bool, residual: float, residual_max: float, pixel_um: dict) -> str | None:
    """What to look at when the answer is not to be trusted, or None."""

    if not accepted:
        return (
            f"the measured mapping sat {residual:.3f} from the nearest whole quarter-turn "
            f"(more than {residual_max}); the rig may be turned by a part of a turn, the "
            f"field may have too little structure, or the stage move may be too small"
        )

    disagree = abs(pixel_um["x"] - pixel_um["y"]) / pixel_um["mean"]

    if disagree > 0.05:
        return (
            f"the pixel size measured along X ({pixel_um['x']:.4f} um) and Y "
            f"({pixel_um['y']:.4f} um) disagree by {disagree:.0%}; the "
            "orientation is trusted but the pixel size should be checked"
        )

    return None
