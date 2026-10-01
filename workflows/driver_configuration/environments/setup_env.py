"""Create the conda environment for the driver_configuration workflow.

One environment, ``ZMART--driver_configuration--main``. Measuring which way
the picture is turned, and where two objectives look, needs numpy, the
registration tools of scikit-image, and the image readers. Matplotlib draws
the review pictures the operator checks the measurement against; no
cellpose, no torch, no GPU.
"""

from __future__ import annotations

import sys
from pathlib import Path

# conda_utils lives in the shared engine, three directories up — on the path
# before it is imported, so this runs as a plain script from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "engine"))

from conda_utils import setup_workflow_env  # noqa: E402


WORKFLOW = "driver_configuration"
PYTHON_VERSION = "3.12"

PIP_PACKAGES = [
    "pyyaml",
    "numpy",
    "scipy",                  # ndimage.zoom and shift for the objective pair
    "scikit-image>=0.23",     # phase_cross_correlation, match_template
    "matplotlib",             # the review pictures
    # Before 2026.6.1 tifffile imports a name zarr 3.3 moved, and the first
    # read fails with a misleading "zarr 3.3.0 < 3 is not supported".
    "tifffile>=2026.6.1",
    "imagecodecs",
    "ngio",          # OME-Zarr, NGFF 0.4 and 0.5
    "ome-types",     # OME-XML metadata
]

#: ``__STEPS__`` is replaced with this workflow's steps directory before the
#: check runs. A placeholder rather than a format field, because these are
#: Python one-liners and braces are theirs.
DIAGNOSTICS = [
    (
        "registration",
        "import numpy as np; from skimage.registration import phase_cross_correlation; "
        "a = np.random.default_rng(0).random((64, 64)); "
        # The answer is the shift that moves the second picture back onto
        # the first: rolled by (3, -2), it comes back by (-3, 2).
        "s, _, _ = phase_cross_correlation(a, np.roll(a, (3, -2), axis=(0, 1))); "
        "print('OK' if tuple(np.round(s)) == (-3.0, 2.0) else 'FAIL shift=' + str(s))",
    ),
    (
        "TIFF/zarr interop",
        "import tifffile, tifffile.zarr, zarr; "
        "print(tifffile.__version__ + ' + zarr ' + zarr.__version__)",
    ),
    (
        "draws a review picture",
        "from matplotlib.figure import Figure; "
        "from matplotlib.backends.backend_agg import FigureCanvasAgg; "
        "f = Figure(); FigureCanvasAgg(f); f.gca().plot([0, 1]); print('OK')",
    ),
    (
        "measures the orientation of a synthetic field",
        "import sys, numpy as np; sys.path.insert(0, r'__STEPS__'); "
        "from measure_orientation import run; "
        "from scipy.ndimage import gaussian_filter; "
        "rng = np.random.default_rng(0); "
        "field = gaussian_filter(rng.random((256, 256)), 2); "
        "home = field[64:192, 64:192]; plus_x = field[64:192, 74:202]; plus_y = field[74:202, 64:192]; "
        "out = run(dict(input=dict(home=home, plus_x=plus_x, plus_y=plus_y, stage_move_um=10.0), "
        "metadata=dict(verbose=0)), dict())['measure_orientation']; "
        "print('OK' if out['accepted'] else 'FAIL ' + str(out))",
    ),
]


if __name__ == "__main__":
    setup_workflow_env(
        workflow=WORKFLOW,
        pip_packages=PIP_PACKAGES,
        diagnostics=DIAGNOSTICS,
        python_version=PYTHON_VERSION,
        install_torch=False,
        steps_dir=Path(__file__).resolve().parents[1] / "steps",
        default_step="main",
    )
