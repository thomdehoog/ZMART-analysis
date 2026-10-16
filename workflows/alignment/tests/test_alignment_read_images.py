"""read_images of the alignment workflow: what it hands on for each kind of input."""

from __future__ import annotations

import numpy as np
import pytest
import tifffile
from helpers import load_step

read_images = load_step("alignment", "read_images")


def _read(given, **params):
    return read_images.run({"input": given, "metadata": {"verbose": 0}}, {}, **params)[
        "read_images"
    ]


def test_a_stack_of_plane_files_keeps_their_order_heights_and_type(tmp_path):
    paths = []
    for z in range(3):
        path = tmp_path / f"plane_{z}.tif"
        tifffile.imwrite(path, np.full((8, 8), z, dtype=np.uint16))
        paths.append(str(path))

    read = _read({"image_paths": paths, "z_um": [10, 11, 12]})

    assert read["stack"].dtype == np.uint16
    assert [int(plane[0, 0]) for plane in read["stack"]] == [0, 1, 2]
    assert read["z_um"] == [10.0, 11.0, 12.0]
    assert read["source"] == f"3 planes from {paths[0]}"


def test_an_ome_tiff_z_step_in_nanometres_is_given_in_micrometres(tmp_path):
    """The heights are in the unit of the pixel width, whatever unit the file keeps z in."""
    path = tmp_path / "stack.ome.tif"
    tifffile.imwrite(
        path,
        np.zeros((3, 8, 8), dtype=np.uint16),
        ome=True,
        metadata={
            "axes": "ZYX",
            "PhysicalSizeX": 0.5,
            "PhysicalSizeXUnit": "µm",
            "PhysicalSizeZ": 2000.0,
            "PhysicalSizeZUnit": "nm",
        },
    )

    assert _read({"image_path": str(path)})["z_um"] == pytest.approx([0.0, 2.0, 4.0])


def test_a_channel_named_with_a_number_is_found_by_its_name(tmp_path):
    """A channel called "488" is that channel, not the channel at index 488."""
    path = tmp_path / "two.ome.tif"
    stack = np.stack([np.zeros((3, 8, 8)), np.ones((3, 8, 8))], axis=1).astype(np.uint16)
    tifffile.imwrite(
        path, stack, ome=True, metadata={"axes": "ZCYX", "Channel": {"Name": ["405", "488"]}}
    )

    assert _read({"image_path": str(path)}, channel="488")["stack"].max() == 1
    assert _read({"image_path": str(path)}, channel="0")["stack"].max() == 0


def test_a_file_holding_several_positions_is_refused(tmp_path):
    path = tmp_path / "two_positions.tif"
    with tifffile.TiffWriter(path) as writer:
        writer.write(np.zeros((8, 8), dtype=np.uint16))
        writer.write(np.zeros((4, 4), dtype=np.uint16))

    with pytest.raises(ValueError, match="positions"):
        _read({"home": str(path), "plus_x": str(path), "plus_y": str(path)})


def test_a_channel_last_array_is_refused():
    rgb = np.zeros((32, 32, 3))
    with pytest.raises(ValueError, match="channel-last"):
        _read({"home": rgb, "plus_x": rgb, "plus_y": rgb})


def test_a_lens_reads_its_picture_on_the_channel_and_its_stack_on_channel_0():
    picture = np.stack([np.zeros((8, 8)), np.ones((8, 8))])
    plane = np.stack([np.full((8, 8), 5.0), np.full((8, 8), 7.0)])
    lens = {"image": picture, "pixel_um": 0.5, "stack": [plane, plane], "z_um": [1.0, 2.0]}

    read = _read({"reference": lens, "target": lens}, channel=1)

    assert read["reference"]["image"].max() == 1.0
    assert read["reference"]["stack"].shape == (2, 8, 8)
    assert read["reference"]["stack"].max() == 5.0
    assert read["reference"]["pixel_um"] == 0.5 and read["reference"]["z_um"] == [1.0, 2.0]
    assert read["channel"] == 1


def test_an_ome_zarr_level_is_found_by_number_from_either_end_and_by_name(tmp_path):
    ngio = pytest.importorskip("ngio")
    path = tmp_path / "position.zarr"
    ngio.create_ome_zarr_from_array(
        str(path),
        np.zeros((1, 1, 3, 16, 16), dtype=np.uint16),
        pixelsize=1.0,
        z_spacing=1.0,
        axes_names=("t", "c", "z", "y", "x"),
        levels=2,
        overwrite=True,
    )

    full = _read({"image_path": str(path)}, level=0)["stack"].shape
    coarse = _read({"image_path": str(path)}, level=-1)["stack"].shape

    assert full == (3, 16, 16) and coarse == (3, 8, 8)
    assert _read({"image_path": str(path)}, level="1")["stack"].shape == coarse
    with pytest.raises(ValueError, match="Levels"):
        _read({"image_path": str(path)}, level="nowhere")
