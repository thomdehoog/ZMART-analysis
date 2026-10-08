"""run_pipeline.py: the command-line runner reads the image's own metadata."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest


def _load_run_pipeline_module():
    path = Path(__file__).resolve().parents[1] / "run_pipeline.py"
    spec = importlib.util.spec_from_file_location("object_analysis_run_pipeline", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_image_size_comes_from_the_image(tmp_path):
    """A (3, 10, 3) image is ambiguous by shape and unambiguous by metadata."""
    import tifffile

    run_pipeline = _load_run_pipeline_module()
    image_path = tmp_path / "declared.tif"
    tifffile.imwrite(image_path, np.zeros((3, 10, 3), dtype=np.uint8), metadata={"axes": "CYX"})

    assert run_pipeline._image_size_px(image_path) == [3, 10]


def test_cli_image_size_refuses_an_undeclared_ambiguous_tiff(tmp_path):
    """Nothing declares the axes, so nothing can honestly resolve them."""
    import tifffile

    run_pipeline = _load_run_pipeline_module()
    image_path = tmp_path / "ambiguous.tif"
    tifffile.imwrite(image_path, np.zeros((3, 10, 3), dtype=np.uint8))

    with pytest.raises(ValueError, match="3-sample .RGB. image"):
        run_pipeline._image_size_px(image_path)
