"""A field with no cells must give an empty selection, not stop the analysis.

This runs the feature-extraction and feedback steps on a small fabricated
image and an all-zero mask, so it needs scikit-image and NumPy but not
Cellpose or a conda environment.
"""

import importlib.util
import json
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("skimage")

STEPS = Path(__file__).parent.parent / "steps"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, STEPS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pipeline_data(masks):
    image = np.random.default_rng(0).random(masks.shape).astype(np.float32)
    return {
        "metadata": {"verbose": 0, "label": "empty_field"},
        "input": {},
        "preprocess": {"image": image},
        "segment": {"masks": masks, "n_cells": int(masks.max())},
    }


def test_no_cells_gives_an_empty_selection(tmp_path):
    data = _pipeline_data(np.zeros((64, 64), dtype=np.int32))

    data = _load("extract_features").run(data, select_by="area", percentile=99)
    assert len(data["extract_features"]["selected_labels"]) == 0
    assert data["extract_features"]["threshold"] is None

    data = _load("feedback").run(data, output_dir=str(tmp_path))
    assert data["feedback"]["n_selected"] == 0
    assert Path(data["feedback"]["filepath"]).exists()
    json.dumps(data)  # the result holds only plain values and paths


def test_cells_are_still_selected_when_present(tmp_path):
    masks = np.zeros((64, 64), dtype=np.int32)
    masks[5:15, 5:15] = 1  # a small cell
    masks[30:60, 30:60] = 2  # a large cell
    data = _pipeline_data(masks)

    data = _load("extract_features").run(data, select_by="area", percentile=50)
    assert list(data["extract_features"]["selected_labels"]) == [2]

    data = _load("feedback").run(data, output_dir=str(tmp_path))
    assert data["feedback"]["n_selected"] == 1
    assert data["feedback"]["cells"][0]["label"] == 2
    json.dumps(data)  # arrays have become files, so the result travels as JSON
    assert np.load(data["feedback"]["masks_path"]).max() == 2
    assert Path(data["feedback"]["properties_path"]).read_text().startswith("label,")
