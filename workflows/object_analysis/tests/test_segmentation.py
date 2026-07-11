"""Tests for shared segmentation channel selection."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _segmentation import select_channels  # noqa: E402


def test_2d_passthrough():
    img = np.zeros((10, 20), dtype=np.uint8)
    assert select_channels(img).shape == (10, 20)


def test_2d_rejects_nonzero_explicit_channel():
    img = np.zeros((10, 20), dtype=np.uint8)
    with pytest.raises(ValueError, match="2D image"):
        select_channels(img, channels=[1])


def test_channel_last_three_kept():
    img = np.zeros((10, 20, 3), dtype=np.uint8)
    assert select_channels(img).shape == (10, 20, 3)


def test_channel_first_normalized_to_last():
    img = np.zeros((3, 10, 20), dtype=np.uint8)
    assert select_channels(img).shape == (10, 20, 3)


def test_single_channel_returns_2d():
    img = np.zeros((10, 20, 1), dtype=np.uint8)
    assert select_channels(img).shape == (10, 20)


def test_more_than_three_uses_specified_indices():
    img = np.zeros((10, 20, 5), dtype=np.uint8)
    img[..., 2] = 7
    img[..., 4] = 9
    out = select_channels(img, channels=[2, 4])
    assert out.shape == (10, 20, 2)
    assert int(out[0, 0, 0]) == 7
    assert int(out[0, 0, 1]) == 9


def test_integer_channel_is_single_channel_shorthand():
    img = np.zeros((10, 20, 3), dtype=np.uint8)
    img[..., 2] = 17
    out = select_channels(img, channels=2)
    assert out.shape == (10, 20)
    assert int(out[0, 0]) == 17


def test_channel_first_more_than_three_uses_specified_indices():
    img = np.zeros((5, 10, 20), dtype=np.uint8)
    img[1] = 11
    img[3] = 13
    out = select_channels(img, channels=[1, 3])
    assert out.shape == (10, 20, 2)
    assert int(out[0, 0, 0]) == 11
    assert int(out[0, 0, 1]) == 13


def test_auto_caps_at_three():
    img = np.zeros((10, 20, 5), dtype=np.uint8)
    assert select_channels(img).shape == (10, 20, 3)


def test_out_of_range_channel_raises():
    img = np.zeros((10, 20, 3), dtype=np.uint8)
    with pytest.raises(ValueError):
        select_channels(img, channels=[0, 9])


def test_more_than_three_requested_raises():
    img = np.zeros((10, 20, 5), dtype=np.uint8)
    with pytest.raises(ValueError):
        select_channels(img, channels=[0, 1, 2, 3])


def test_empty_channel_list_raises_for_multichannel():
    img = np.zeros((10, 20, 3), dtype=np.uint8)
    with pytest.raises(ValueError, match="at least one"):
        select_channels(img, channels=[])


def test_four_dimensional_input_raises():
    img = np.zeros((2, 3, 10, 20), dtype=np.uint8)
    with pytest.raises(ValueError):
        select_channels(img)


def test_equal_end_axes_are_ambiguous_and_rejected():
    # (C, H, W) and (H, W, C) are indistinguishable when the two end axes
    # are equal, so inference must refuse rather than silently guess.
    img = np.zeros((3, 10, 3), dtype=np.uint8)
    with pytest.raises(ValueError, match="infer channel axis"):
        select_channels(img)


def test_explicit_channel_axis_resolves_ambiguous_shape():
    # With equal end axes, an explicit orientation disambiguates. Probe two
    # points that live in channel 0 under exactly one orientation each.
    img = np.zeros((3, 10, 3), dtype=np.uint8)
    img[0, 5, 1] = 5   # in the first-axis plane 0, not the last-axis plane 0
    img[1, 5, 0] = 7   # in the last-axis plane 0, not the first-axis plane 0

    first = select_channels(img, channels=[0], channel_axis=0)
    last = select_channels(img, channels=[0], channel_axis=-1)
    assert first.shape == (10, 3)   # channel-first (C, H, W) -> (H, W)
    assert last.shape == (3, 10)    # channel-last (H, W, C) -> (H, W)
    assert int(first[5, 1]) == 5
    assert int(last[1, 5]) == 7


def test_explicit_channel_axis_first_matches_inference():
    img = np.zeros((3, 10, 20), dtype=np.uint8)
    img[2] = 9
    explicit = select_channels(img, channel_axis=0)
    inferred = select_channels(img)
    assert explicit.shape == inferred.shape == (10, 20, 3)
    assert int(explicit[0, 0, 2]) == int(inferred[0, 0, 2]) == 9


def test_explicit_channel_axis_last_keeps_orientation():
    img = np.zeros((10, 20, 3), dtype=np.uint8)
    for axis_value in (2, -1):
        out = select_channels(img, channel_axis=axis_value)
        assert out.shape == (10, 20, 3)


def test_invalid_channel_axis_raises():
    img = np.zeros((3, 10, 20), dtype=np.uint8)
    with pytest.raises(ValueError, match="channel_axis must be"):
        select_channels(img, channel_axis=1)


def test_immunohistochemistry_keeps_three_channels():
    from skimage.data import immunohistochemistry

    try:
        image = immunohistochemistry()
    except Exception as exc:
        pytest.skip(f"skimage immunohistochemistry fixture unavailable: {exc}")

    out = select_channels(image)
    assert out.shape == (512, 512, 3)


class _RecordingModel:
    def __init__(self):
        self.calls = []

    def eval(self, x, channel_axis=None, **kwargs):
        self.calls.append(
            {
                "input": np.asarray(x).copy(),
                "shape": x.shape,
                "channel_axis": channel_axis,
                "kwargs": kwargs,
            }
        )
        masks = np.zeros(x.shape[:2], dtype=np.int32)
        masks[1:3, 1:3] = 1
        return masks, None, None


class _PositionModel:
    def eval(self, x, channel_axis=None, **kwargs):
        masks = np.zeros(x.shape[:2], dtype=np.int32)
        masks[2, 3] = 1
        return masks, None, None


class _CornerPixelModel:
    def eval(self, x, channel_axis=None, **kwargs):
        masks = np.zeros(x.shape[:2], dtype=np.int32)
        masks[0, 0] = 1
        return masks, None, None


class _AreaModel:
    def eval(self, x, channel_axis=None, **kwargs):
        masks = np.zeros(x.shape[:2], dtype=np.int32)
        masks[1:3, 1:3] = 1
        masks[4:9, 4:9] = 2
        masks[10:20, 10:20] = 3
        return masks, None, None


def test_segment_tiff_uses_channel_axis_for_multichannel(tmp_path):
    import tifffile
    from _segmentation import segment_tiff

    path = tmp_path / "rgb.tif"
    tifffile.imwrite(path, np.zeros((10, 12, 3), dtype=np.uint8))
    model = _RecordingModel()
    out = segment_tiff(path, {"model": model})

    assert model.calls[0]["channel_axis"] == -1
    assert "diameter" not in model.calls[0]["kwargs"]
    assert out["image_2d"].ndim == 2


def test_segment_tiff_explicit_axis_resolves_ambiguous_tiff(tmp_path):
    import tifffile
    from _segmentation import segment_tiff

    path = tmp_path / "ambiguous.tif"
    image = np.zeros((3, 10, 3), dtype=np.uint8)
    image[0, 5, 1] = 17
    tifffile.imwrite(path, image)
    model = _RecordingModel()

    out = segment_tiff(
        path,
        {"model": model},
        channels=[0],
        channel_axis=0,
        gpu=False,
    )

    assert out["image"].shape == (10, 3)
    assert int(out["image"][5, 1]) == 17
    assert model.calls[0]["shape"] == (10, 3)


def test_segment_tiff_no_channel_axis_for_2d(tmp_path):
    import tifffile
    from _segmentation import segment_tiff

    path = tmp_path / "gray.tif"
    tifffile.imwrite(path, np.zeros((10, 12), dtype=np.uint8))
    model = _RecordingModel()
    segment_tiff(path, {"model": model})

    assert model.calls[0]["channel_axis"] is None


def test_segment_tiff_filters_masks_by_min_and_max_area(tmp_path):
    import tifffile
    from _segmentation import segment_tiff

    path = tmp_path / "gray.tif"
    tifffile.imwrite(path, np.zeros((24, 24), dtype=np.uint8))
    out = segment_tiff(
        path,
        {"model": _AreaModel()},
        min_area_px=10,
        max_area_px=50,
    )

    assert out["n_raw_objects"] == 3
    assert out["n_objects"] == 1
    assert out["dropped_labels"] == [1, 3]
    assert out["area_filter"] == {"min_area_px": 10, "max_area_px": 50}
    assert np.unique(out["masks"]).tolist() == [0, 1]
    assert int((out["masks"] == 1).sum()) == 25


def test_segment_tiff_binning_downsamples_cellpose_input_without_upsampling(tmp_path):
    import tifffile
    from _segmentation import segment_tiff

    path = tmp_path / "large.tif"
    tifffile.imwrite(path, np.zeros((20, 40), dtype=np.uint8))
    model = _RecordingModel()
    out = segment_tiff(path, {"model": model}, segmentation_binning=4)

    assert model.calls[0]["shape"] == (5, 10)
    assert model.calls[0]["input"].dtype == np.float32
    assert out["masks"].shape == (20, 40)
    assert out["segmentation_resize"]["binning"] == 4
    assert out["segmentation_resize"]["input_size_px"] == [10, 5]
    assert out["segmentation_resize"]["scale"] == 0.25

    model = _RecordingModel()
    segment_tiff(path, {"model": model}, segmentation_binning=1)
    assert model.calls[0]["shape"] == (20, 40)


def test_segment_tiff_uses_segmentation_binning(tmp_path):
    import tifffile
    from _segmentation import segment_tiff

    path = tmp_path / "large.tif"
    tifffile.imwrite(path, np.zeros((20, 40), dtype=np.uint8))
    model = _RecordingModel()
    out = segment_tiff(path, {"model": model}, segmentation_binning=4)

    assert model.calls[0]["shape"] == (5, 10)
    assert out["masks"].shape == (20, 40)
    assert out["segmentation_resize"]["binning"] == 4
    assert out["segmentation_resize"]["input_size_px"] == [10, 5]


def test_segment_tiff_area_downsamples_intensity_image(tmp_path):
    import tifffile
    from _segmentation import segment_tiff

    path = tmp_path / "small.tif"
    image = np.arange(16, dtype=np.uint16).reshape(4, 4)
    tifffile.imwrite(path, image)
    model = _RecordingModel()
    segment_tiff(path, {"model": model}, segmentation_binning=2)

    expected = np.array([[2.5, 4.5], [10.5, 12.5]], dtype=np.float32)
    np.testing.assert_allclose(model.calls[0]["input"], expected)


def test_segment_tiff_binned_masks_are_not_smoothed(tmp_path):
    import tifffile
    from _segmentation import segment_tiff

    path = tmp_path / "large.tif"
    tifffile.imwrite(path, np.zeros((8, 8), dtype=np.uint8))
    out = segment_tiff(
        path,
        {"model": _CornerPixelModel()},
        segmentation_binning=4,
    )

    assert int((out["raw_masks"] == 1).sum()) == 16
    assert "mask_smoothing_sigma_px" not in out["segmentation_resize"]


def test_segment_tiff_upscaled_mask_position_is_original_space(tmp_path):
    import tifffile
    from _segmentation import segment_tiff

    path = tmp_path / "large.tif"
    tifffile.imwrite(path, np.zeros((20, 40), dtype=np.uint8))
    out = segment_tiff(path, {"model": _PositionModel()}, segmentation_binning=4)

    rows, cols = np.where(out["masks"] == 1)
    assert rows.min() == 8
    assert rows.max() == 11
    assert cols.min() == 12
    assert cols.max() == 15


def test_segment_tiff_passes_cellpose_tuning_params(tmp_path):
    import tifffile
    from _segmentation import segment_tiff

    path = tmp_path / "gray.tif"
    tifffile.imwrite(path, np.zeros((24, 24), dtype=np.uint8))
    model = _RecordingModel()
    out = segment_tiff(
        path,
        {"model": model},
        cellprob_threshold=-1.0,
        flow_threshold=0.7,
        niter=2000,
        diameter=90,
        gpu=False,
    )

    assert model.calls[0]["kwargs"] == {
        "cellprob_threshold": -1.0,
        "flow_threshold": 0.7,
        "niter": 2000,
        "diameter": 90.0,
    }
    assert out["cellpose_params"] == {
        "requested_gpu": False,
        "used_gpu": False,
        "device": "cpu",
        "cellprob_threshold": -1.0,
        "flow_threshold": 0.7,
        "niter": 2000,
        "diameter": 90.0,
    }


def test_segment_tiff_prefers_gpu_and_falls_back_to_cpu(tmp_path, monkeypatch):
    import tifffile
    from _segmentation import segment_tiff

    path = tmp_path / "gray.tif"
    tifffile.imwrite(path, np.zeros((8, 8), dtype=np.uint8))
    calls = []

    class _FakeDevice:
        def __init__(self, name):
            self.type = name
            self.name = name

    class _FakeCuda:
        @staticmethod
        def is_available():
            return True

    class _FakeMps:
        @staticmethod
        def is_available():
            return False

    class _FakeTorch:
        cuda = _FakeCuda()
        backends = types.SimpleNamespace(mps=_FakeMps())

        @staticmethod
        def device(name):
            return _FakeDevice(name)

    class _FakeCellposeModel:
        def __init__(self, gpu=False, device=None):
            device_name = getattr(device, "type", "cpu")
            calls.append((bool(gpu), device_name))
            if device_name == "cuda":
                raise RuntimeError("no cuda for test")
            self.device_name = device_name

        def eval(self, x, channel_axis=None, **kwargs):
            masks = np.zeros(x.shape[:2], dtype=np.int32)
            masks[1:4, 1:4] = 1
            return masks, None, None

    fake_models = types.SimpleNamespace(CellposeModel=_FakeCellposeModel)
    monkeypatch.setitem(sys.modules, "torch", _FakeTorch)
    monkeypatch.setitem(sys.modules, "cellpose", types.SimpleNamespace(models=fake_models))

    out = segment_tiff(path, {}, gpu=True)

    assert calls == [(True, "cuda"), (False, "cpu")]
    assert out["cellpose_params"]["requested_gpu"] is True
    assert out["cellpose_params"]["used_gpu"] is False
    assert out["cellpose_params"]["device"] == "cpu"


def test_segment_tiff_uses_mps_when_cuda_is_unavailable(tmp_path, monkeypatch):
    import tifffile
    from _segmentation import segment_tiff

    path = tmp_path / "gray.tif"
    tifffile.imwrite(path, np.zeros((8, 8), dtype=np.uint8))
    calls = []

    class _FakeDevice:
        def __init__(self, name):
            self.type = name

    class _FakeCuda:
        @staticmethod
        def is_available():
            return False

    class _FakeMps:
        @staticmethod
        def is_available():
            return True

    class _FakeTorch:
        cuda = _FakeCuda()
        backends = types.SimpleNamespace(mps=_FakeMps())

        @staticmethod
        def device(name):
            return _FakeDevice(name)

    class _FakeCellposeModel:
        def __init__(self, gpu=False, device=None):
            device_name = getattr(device, "type", "cpu")
            calls.append((bool(gpu), device_name))
            self.device_name = device_name

        def eval(self, x, channel_axis=None, **kwargs):
            masks = np.zeros(x.shape[:2], dtype=np.int32)
            masks[1:4, 1:4] = 1
            return masks, None, None

    fake_models = types.SimpleNamespace(CellposeModel=_FakeCellposeModel)
    monkeypatch.setitem(sys.modules, "torch", _FakeTorch)
    monkeypatch.setitem(sys.modules, "cellpose", types.SimpleNamespace(models=fake_models))

    out = segment_tiff(path, {}, gpu=True)

    assert calls == [(True, "mps")]
    assert out["cellpose_params"]["requested_gpu"] is True
    assert out["cellpose_params"]["used_gpu"] is True
    assert out["cellpose_params"]["device"] == "mps"


def test_segment_tiff_rejects_invalid_area_filter(tmp_path):
    import tifffile
    from _segmentation import segment_tiff

    path = tmp_path / "gray.tif"
    tifffile.imwrite(path, np.zeros((24, 24), dtype=np.uint8))
    with pytest.raises(ValueError, match="max_area_px"):
        segment_tiff(
            path,
            {"model": _AreaModel()},
            min_area_px=100,
            max_area_px=10,
        )
