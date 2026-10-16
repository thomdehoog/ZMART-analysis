"""The alignment pipelines on real microscopy, from scikit-image's sample data.

- ``cells3d``: a confocal stack of nuclei, 60 planes 0.29 um apart.
- ``kidney``: a confocal stack of a kidney section, 16 planes.
- ``human_mitosis``: a field of nuclei, for orientation.

The samples are downloaded on first use and cached; without a network the
tests are skipped.
"""

from __future__ import annotations

import numpy as np
import pytest
import score_focus
from helpers import load_step
from scipy.ndimage import zoom

read_images = load_step("alignment", "read_images")
measure_orientation = load_step("alignment", "measure_orientation")
measure_registration = load_step("alignment", "measure_registration")

pytestmark = pytest.mark.pooch


def _sample(name):
    data = pytest.importorskip("skimage.data")
    try:
        return getattr(data, name)()
    except Exception as exc:
        pytest.skip(f"skimage sample {name} unavailable: {exc}")


def _through(step, given, **params):
    """read_images, then the step: the pipeline in this process."""
    reader = {key: params.pop(key) for key in ("channel", "level", "t") if key in params}
    data = read_images.run({"input": given, "metadata": {"verbose": 0}}, {}, **reader)
    return step.run(data, {}, **params)


@pytest.fixture(scope="module")
def cells3d_position(tmp_path_factory):
    """cells3d as one OME-Zarr position: membranes in channel 0, nuclei in channel 1."""
    ngio = pytest.importorskip("ngio")
    cells = _sample("cells3d")
    path = tmp_path_factory.mktemp("cells3d") / "cells3d.zarr"
    ngio.create_ome_zarr_from_array(
        str(path),
        np.moveaxis(cells, 1, 0)[None],
        pixelsize=0.26,
        z_spacing=0.29,
        axes_names=("t", "c", "z", "y", "x"),
        levels=1,
        overwrite=True,
    )
    return path


def test_the_nuclei_of_cells3d_are_sharpest_in_the_middle_of_the_stack(cells3d_position):
    found = _through(score_focus, {"image_path": str(cells3d_position)}, channel=1)["score_focus"]

    assert found["found"] is True
    assert found["n_planes"] == 60
    assert found["peak_z_um"] == pytest.approx(9.73, abs=0.3)


def test_the_same_stack_written_one_plane_per_file_focuses_the_same(cells3d_position, tmp_path):
    import tifffile

    cells = _sample("cells3d")
    paths = []
    for z in range(60):
        path = tmp_path / f"plane_{z:02d}.tif"
        tifffile.imwrite(path, cells[z, 1])
        paths.append(str(path))

    planes = _through(score_focus, {"image_paths": paths, "z_um": [0.29 * z for z in range(60)]})
    position = _through(score_focus, {"image_path": str(cells3d_position)}, channel=1)

    assert planes["score_focus"]["metrics"] == position["score_focus"]["metrics"]


def test_the_kidney_stack_is_sharpest_near_its_middle(tmp_path):
    ngio = pytest.importorskip("ngio")
    kidney = _sample("kidney")
    path = tmp_path / "kidney.zarr"
    ngio.create_ome_zarr_from_array(
        str(path),
        np.moveaxis(kidney, -1, 0)[None],
        pixelsize=0.5,
        z_spacing=1.0,
        axes_names=("t", "c", "z", "y", "x"),
        levels=1,
        overwrite=True,
    )

    found = _through(score_focus, {"image_path": str(path)}, channel=0, skip_ends=1)["score_focus"]

    assert found["found"] is True
    assert found["peak_z_um"] == pytest.approx(6.98, abs=0.5)


@pytest.mark.parametrize("rotation_deg", [0, 90, 180, 270])
@pytest.mark.parametrize("reflection", [False, True])
def test_every_layout_is_found_on_real_nuclei(rotation_deg, reflection):
    field = _sample("human_mitosis").astype(np.float64)
    move_px, pixel_um = 24, 0.5

    def recorded(rows, cols):
        """What a camera turned this way records of one window of the field."""
        window = np.rot90(field[rows, cols], k=rotation_deg // 90)
        return np.ascontiguousarray(np.fliplr(window) if reflection else window)

    given = {
        "home": recorded(slice(100, 356), slice(100, 356)),
        "plus_x": recorded(slice(100, 356), slice(100 + move_px, 356 + move_px)),
        "plus_y": recorded(slice(100 + move_px, 356 + move_px), slice(100, 356)),
        "stage_move_um": move_px * pixel_um,
    }

    found = _through(measure_orientation, given)["measure_orientation"]

    assert found["accepted"], found["why"]
    assert found["orientation"]["rotation_deg"] == rotation_deg
    assert found["orientation"]["reflection"] is reflection
    assert found["pixel_um"]["mean"] == pytest.approx(pixel_um, abs=0.01)


def test_two_lenses_on_real_nuclei_are_registered_in_x_y_and_z():
    """The target lens has half the pixel size, looks 12 by 8 pixels away, and its stack reads 1 um higher."""
    nuclei = _sample("cells3d")[:, 1].astype(np.float64)
    reference = nuclei[30]
    target = zoom(np.roll(reference, (-8, -12), axis=(0, 1))[64:192, 64:192], 2.0, order=1)
    heights = [0.29 * z for z in range(20, 41)]
    given = {
        "reference": {
            "image": reference,
            "pixel_um": 0.26,
            "stack": [nuclei[z] for z in range(20, 41)],
            "z_um": heights,
        },
        "target": {
            "image": target,
            "pixel_um": 0.13,
            "stack": [zoom(nuclei[z][64:192, 64:192], 2.0, order=1) for z in range(20, 41)],
            "z_um": [height + 1.0 for height in heights],
        },
    }

    found = _through(measure_registration, given)["measure_registration"]

    assert found["accepted"], found["why"]
    assert found["translation_um"]["x"] == pytest.approx(12 * 0.26, abs=0.05)
    assert found["translation_um"]["y"] == pytest.approx(8 * 0.26, abs=0.05)
    assert found["translation_um"]["z"] == pytest.approx(1.0, abs=0.3)
