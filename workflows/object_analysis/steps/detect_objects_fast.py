"""detect_objects_fast -- the watershed detector, in the classical environment.

The same detection step as ``detect_objects``, with ``method`` fixed to
``fast``: a background-subtracted watershed built from scikit-image, with no
deep-learning model. It lives in its own file because a step file owns its
environment, and this detector needs only scipy and scikit-image. Running it
in the Cellpose environment would start a torch worker it never uses.

Takes and returns exactly what ``detect_objects`` does, so the feature and
table steps after it cannot tell which detector ran, except from
``detector_params["method"]``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from detect_objects import run as _detect  # noqa: E402

METADATA = {
    "description": "Watershed object detection, without a deep-learning model",
    "version": "1.0",
    "max_workers": 1,
    "environment": "ZMART--object_analysis--classical",
}


def run(pipeline_data: dict, state: dict, **params) -> dict:
    params = dict(params)
    requested = params.pop("method", "fast")
    if requested != "fast":
        raise ValueError(
            f"detect_objects_fast only runs the fast detector, got method={requested!r}; "
            "use detect_objects for Cellpose."
        )
    pipeline_data = _detect(pipeline_data, state, method="fast", **params)
    # Downstream steps read the detection under the name `detect_objects`,
    # which this step keeps, so the rest of the pipeline is unchanged.
    return pipeline_data
