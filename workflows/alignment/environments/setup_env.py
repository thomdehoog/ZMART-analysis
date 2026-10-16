"""Create the conda environment for the alignment workflow.

One environment, ``ZMART--alignment--main``, for focus, orientation and
registration: numpy and scipy for the measures, scikit-image for the
registration, and the image readers. No cellpose, no torch, no GPU.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The repository root goes on the path first, so this runs as a plain script
# from anywhere, whether or not the package has been installed.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from zmart_analysis.environments import setup_workflow_env  # noqa: E402

WORKFLOW = "alignment"
PYTHON_VERSION = "3.12"

PIP_PACKAGES = [
    "pyyaml",
    "numpy",
    "scipy",  # the DCT of the focus measure, and zoom for the registration
    "scikit-image>=0.23",  # phase_cross_correlation, match_template
    # Before 2026.6.1 tifffile imports a name zarr 3.3 moved, and the first
    # read fails with a misleading "zarr 3.3.0 < 3 is not supported".
    "tifffile>=2026.6.1",
    "imagecodecs",
    "ngio",  # OME-Zarr, NGFF 0.4 and 0.5
    "ome-types",  # OME-XML metadata
]

#: ``__STEPS__`` is replaced with this workflow's steps directory before the
#: check runs. A placeholder rather than a format field, because these are
#: Python one-liners and braces are theirs.
DIAGNOSTICS = [
    (
        "TIFF/zarr interop",
        "import tifffile, tifffile.zarr, zarr; "
        "print(tifffile.__version__ + ' + zarr ' + zarr.__version__)",
    ),
    (
        "focus on an OME-Zarr stack",
        "import sys, tempfile, numpy as np, ngio; "
        "from pathlib import Path; "
        "d = Path(tempfile.mkdtemp()) / 'p.zarr'; "
        "a = np.zeros((1, 1, 5, 32, 32), dtype='uint16'); "
        "a[0, 0, 2] = np.random.default_rng(0).integers(0, 4096, size=(32, 32)); "
        "ngio.create_ome_zarr_from_array(d, a, pixelsize=1.0, z_spacing=1.0, "
        "axes_names=('t','c','z','y','x'), levels=1, overwrite=True); "
        "sys.path.insert(0, r'__STEPS__'); "
        "import read_images, score_focus; "
        "data = read_images.run(dict(input=dict(image_path=str(d)), metadata=dict(verbose=0)), dict()); "
        "peak = score_focus.run(data, dict(), skip_ends=0)['score_focus']['peak_index']; "
        "print('OK' if abs(peak - 2) < 0.5 else 'FAIL peak=' + str(peak))",
    ),
    (
        "orientation of a test field",
        "import sys, numpy as np; sys.path.insert(0, r'__STEPS__'); "
        "import read_images, measure_orientation; "
        "from scipy.ndimage import gaussian_filter; "
        "rng = np.random.default_rng(0); "
        "field = gaussian_filter(rng.random((256, 256)), 2); "
        "home = field[64:192, 64:192]; plus_x = field[64:192, 74:202]; plus_y = field[74:202, 64:192]; "
        "data = read_images.run(dict(input=dict(home=home, plus_x=plus_x, plus_y=plus_y, "
        "stage_move_um=10.0), metadata=dict(verbose=0)), dict()); "
        "out = measure_orientation.run(data, dict())['measure_orientation']; "
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
