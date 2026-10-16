"""Create the conda environments of the object_analysis workflow.

Three, so each step starts only what it needs:

``cellpose``   segment_cellpose: PyTorch and Cellpose, on the GPU when there is one;
               no image readers, since read_images runs in ``classical``
``classical``  read_images, segment_watershed, measure_objects, build_object_table
``umap``       reduce_dimensions: scikit-learn and UMAP

Each check at the end runs the environment's own steps on a small image.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# The repository root goes on the path first, so this runs as a plain script
# from anywhere, whether or not the package has been installed.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from zmart_analysis.environments import setup_workflow_env  # noqa: E402

WORKFLOW = "object_analysis"
PYTHON_VERSION = "3.12"

#: Four bright blobs on a dark field, the picture every check segments.
BLOBS = (
    "; ".join(
        [
            "import sys, tempfile, numpy as np, tifffile",
            "from pathlib import Path",
            "sys.path.insert(0, r'__STEPS__')",
            "yy, xx = np.mgrid[0:96, 0:96]",
            "plane = sum(1500.0 * np.exp(-((yy - y) ** 2 + (xx - x) ** 2) / 72.0)"
            " for y, x in [(24, 24), (24, 72), (72, 24), (72, 72)]).astype('uint16')",
            "given = dict(image_path='blobs.tif', tile_id=['T', 0, 0], tile_stage_xy_um=[0.0, 0.0],"
            " source_pixel_size_um=[1.0, 1.0], image_to_stage=[[1, 0], [0, 1]])",
        ]
    )
    + "; "
)

#: The blobs written as a TIFF and read by read_images, as a pipeline does.
BLOBS_READ = (
    BLOBS
    + "; ".join(
        [
            "path = Path(tempfile.mkdtemp()) / 'blobs.tif'",
            "tifffile.imwrite(path, plane)",
            "given['image_path'] = str(path)",
            "import read_images",
            "data = read_images.run(dict(input=given, metadata=dict(verbose=0)), dict())",
        ]
    )
    + "; "
)

#: The blobs handed on as read_images hands them on, for an environment
#: without the image readers.
BLOBS_HANDED_ON = BLOBS + (
    "data = dict(input=given, metadata=dict(verbose=0), read_images=dict(image=plane,"
    " extra_channels=[], channels=None, z_selection='mid', image_size_px=[96, 96], sha256='')); "
)

STEP_PROFILES = {
    "cellpose": {
        "description": "Cellpose detection",
        "install_torch": True,
        "pip_packages": [
            "pyyaml",
            "numpy",
            # The masks are written as TIFF files
            "tifffile",
            # Pinned to the network the masks are made with, cpsam: 4.2
            # made cpsam_v2 the default, and the masks change with the
            # network (see detector_params). Moving to it is a decision.
            "cellpose>=4,<4.2",
        ],
        "diagnostics": [
            ("cellpose", "from cellpose import models; print('OK')"),
            (
                "Cellpose on four blobs",
                BLOBS_HANDED_ON + "import segment_cellpose; "
                "found = segment_cellpose.run(data, dict(), diameter=12, gpu=True)['segment_cellpose']; "
                "print(('OK ' if found['n_objects'] == 4 else 'FAIL ') "
                "+ str(found['n_objects']) + ' objects on ' + found['detector_params']['device'])",
            ),
        ],
    },
    "classical": {
        "description": "reading the image, the watershed, the features and the table",
        "install_torch": False,
        "pip_packages": [
            "pyyaml",
            "numpy",
            "scikit-image>=0.23",
            "tifffile>=2026.6.1",
            "imagecodecs",
            "ngio",  # OME-Zarr, NGFF 0.4 and 0.5
            "ome-types",  # OME-XML metadata
        ],
        "diagnostics": [
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
            (
                "watershed steps, four blobs",
                BLOBS_READ + "import segment_watershed, measure_objects, build_object_table; "
                "data = segment_watershed.run(data, dict(), diameter=12, threshold=100); "
                "data = measure_objects.run(data, dict(), extras=['all']); "
                "table = build_object_table.run(data, dict())['object_analysis']; "
                "n = table['objects']['n_objects']; "
                "print(('OK ' if n == 4 else 'FAIL ') + str(n) + ' objects')",
            ),
        ],
    },
    "umap": {
        "description": "two axes for a population (reduce_dimensions): PCA and UMAP",
        "install_torch": False,
        # Only reduce_dimensions runs here. It reads the object table the
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
                "from reduce_dimensions import run; "
                "got = run(dict(input=dict(table=str(d), kind='umap', ids=None), metadata=dict(verbose=0)), dict()); "
                "print('OK' if got['reduce_dimensions']['objects'] == 40 else 'FAIL')",
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
