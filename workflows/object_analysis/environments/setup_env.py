"""Create conda environments for the object_analysis workflow."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# conda_utils lives in the shared engine, three directories up — on the path
# before it is imported, so this runs as a plain script from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "engine"))

from conda_utils import setup_workflow_env  # noqa: E402

WORKFLOW = "object_analysis"
PYTHON_VERSION = "3.12"

STEP_PROFILES = {
    "cellpose": {
        "description": "Cellpose detection",
        "install_torch": True,
        "pip_packages": [
            "pyyaml",
            "numpy",
            # tifffile exposes a TIFF as a zarr array, which is how OME-TIFF
            # and OME-Zarr reach the same plane selection. Versions before
            # 2026.6.1 import a name zarr 3.3 moved, and fail on the first
            # read with a misleading "zarr 3.3.0 < 3 is not supported".
            "tifffile>=2026.6.1",
            "imagecodecs",
            "pooch",
            "ngio",  # OME-Zarr, NGFF 0.4 and 0.5
            "ome-types",  # OME-XML metadata
            # Pinned to one major version: the default network and what
            # `diameter: null` means both changed between 3 and 4, and a
            # table must say which produced it (see detector_params).
            "cellpose>=4,<5",
            "scikit-image>=0.23",  # the fast detector's watershed
        ],
        "diagnostics": [
            (
                "TIFF/zarr interop",
                "import tifffile, tifffile.zarr, zarr; "
                "print(f'tifffile {tifffile.__version__} + zarr {zarr.__version__}')",
            ),
            (
                "reads an OME-Zarr position",
                "import tempfile, numpy as np, ngio; "
                "from pathlib import Path; "
                "d = Path(tempfile.mkdtemp()) / 'p.zarr'; "
                "ngio.create_ome_zarr_from_array("
                "    d, np.zeros((1, 1, 2, 8, 8), dtype='uint16'), pixelsize=1.0, "
                "    axes_names=('t','c','z','y','x'), levels=1, overwrite=True); "
                "c = ngio.open_ome_zarr_container(str(d), mode='r'); "
                "print('OK')",
            ),
            ("OME-XML metadata", "import ome_types; print('OK')"),
            ("cellpose", "from cellpose import models; print('OK')"),
        ],
    },
    "classical": {
        "description": "scikit-image classical feature extraction and the fast detector",
        "install_torch": False,
        "pip_packages": [
            "pyyaml",
            "numpy",
            "scikit-image>=0.23",
            # The fast detector runs here too (object_analysis_fast.yaml),
            # and it reads the position and writes its masks the same way
            # the cellpose one does: the same readers, torch left out.
            "tifffile>=2026.6.1",
            "imagecodecs",
            "ngio",  # OME-Zarr, NGFF 0.4 and 0.5
            "ome-types",  # OME-XML metadata
            # The well and plate summaries (object_analysis_scoped.yaml):
            # tables, median imputation, robust scaling and PCA.
            "pandas",
            "scikit-learn",
        ],
        "diagnostics": [
            (
                "the well and plate summaries",
                "import pandas; from sklearn.decomposition import PCA; print('OK')",
            ),
            (
                "scikit-image",
                "from skimage.measure import regionprops_table; "
                "import numpy as np; "
                "m = np.zeros((8, 8), dtype=np.int32); "
                "m[2:4, 2:4] = 1; "
                "regionprops_table(m, properties=('label', 'area')); "
                "print('OK')",
            ),
            (
                "the fast detector's watershed",
                "from skimage import filters, measure, morphology, segmentation; "
                "from scipy import ndimage; print('OK')",
            ),
            (
                "TIFF/zarr interop",
                "import tifffile, tifffile.zarr, zarr; "
                "print(f'tifffile {tifffile.__version__} + zarr {zarr.__version__}')",
            ),
            (
                "reads an OME-Zarr position",
                "import tempfile, numpy as np, ngio; "
                "from pathlib import Path; "
                "d = Path(tempfile.mkdtemp()) / 'p.zarr'; "
                "ngio.create_ome_zarr_from_array("
                "    d, np.zeros((1, 1, 2, 8, 8), dtype='uint16'), pixelsize=1.0, "
                "    axes_names=('t','c','z','y','x'), levels=1, overwrite=True); "
                "c = ngio.open_ome_zarr_container(str(d), mode='r'); "
                "print('OK')",
            ),
            ("OME-XML metadata", "import ome_types; print('OK')"),
        ],
    },
    "umap": {
        "description": "the population plots (plot_population): PCA and UMAP",
        "install_torch": False,
        # Only plot_population runs here. It reads the object table the
        # other steps wrote, so it needs no image readers and no torch; it
        # gets its own environment because umap-learn brings numba along,
        # a heavy compiler the classical steps have no use for.
        "pip_packages": [
            "pyyaml",
            "numpy",
            "pandas",  # the population table
            "scikit-learn",  # principal components
            "umap-learn",  # the UMAP layout, through numba and pynndescent
        ],
        "diagnostics": [
            ("principal components", "from sklearn.decomposition import PCA; print('OK')"),
            ("UMAP", "import umap; print('umap ' + umap.__version__)"),
            (
                "plots a small population",
                # ``__STEPS__`` is replaced with this workflow's steps
                # directory before the check runs.
                "import sys, csv, tempfile, numpy as np; from pathlib import Path; "
                "d = Path(tempfile.mkdtemp()) / 'overview_abc123_objects.csv'; "
                "rng = np.random.default_rng(0); "
                "rows = [['id', 'area', 'solidity']] + [[f'c{i}', rng.normal(), rng.normal()] for i in range(40)]; "
                "csv.writer(d.open('w', newline='')).writerows(rows); "
                "sys.path.insert(0, r'__STEPS__'); "
                "from plot_population import run; "
                "got = run(dict(input=dict(table=str(d), kind='umap', ids=None), metadata=dict(verbose=0)), dict()); "
                "print('OK' if got['plot_population']['objects'] == 40 else 'FAIL')",
            ),
        ],
    },
}


def _selected_profile() -> dict:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--step", default="cellpose")
    args, _ = parser.parse_known_args()
    if args.step not in STEP_PROFILES:
        expected = ", ".join(sorted(STEP_PROFILES))
        raise SystemExit(f"Unknown --step {args.step!r}. Expected one of: {expected}")
    return STEP_PROFILES[args.step]


if __name__ == "__main__":
    profile = _selected_profile()
    setup_workflow_env(
        workflow=WORKFLOW,
        pip_packages=profile["pip_packages"],
        diagnostics=profile["diagnostics"],
        python_version=PYTHON_VERSION,
        install_torch=profile["install_torch"],
        steps_dir=Path(__file__).resolve().parents[1] / "steps",
        default_step="cellpose",
    )
