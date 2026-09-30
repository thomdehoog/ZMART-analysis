"""summarise_well and summarise_plate: what a well and a plate report.

The tiles are object tables as build_object_table publishes them, made up
here so the numbers are known: two wells of ordinary cells and one well
whose cells are twice as large, so the plate step must single it out.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("pandas")
pytest.importorskip("sklearn")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "steps"))
from summarise_plate import run as plate_run  # noqa: E402
from summarise_well import run as well_run  # noqa: E402


def _tile(rng, n, area_mean, tile):
    area = rng.normal(area_mean, area_mean * 0.1, n)
    props = {
        "label": list(range(1, n + 1)),
        "object_id": [f"{tile}_obj{i:05d}" for i in range(1, n + 1)],
        "tile_name": [tile] * n,
        "centroid_row_px": rng.uniform(0, 100, n).tolist(),
        "stage_x_um": rng.uniform(0, 100, n).tolist(),
        "area": area.tolist(),
        "intensity_mean": rng.normal(500, 50, n).tolist(),
        "eccentricity": rng.uniform(0.1, 0.6, n).tolist(),
        "solidity": rng.uniform(0.85, 0.99, n).tolist(),
    }
    return {"object_analysis": {"objects": {"properties": props, "n_objects": n}}}


def _well(plate, well, area_mean, seed, **params):
    rng = np.random.default_rng(seed)
    tiles = [_tile(rng, 30, area_mean, f"{well}_t{t}") for t in range(3)]
    data = {"results": tiles, "failures": [],
            "metadata": {"scope": {"plate": plate, "well": well}}}
    return well_run(data, {}, **params)


def test_a_well_joins_its_tiles_into_one_population():
    got = _well("P1", "A1", 100.0, 0)["well_population"]
    assert got["n_tiles"] == 3
    assert got["n_objects"] == 90
    assert got["features"] == ["area", "eccentricity", "intensity_mean", "solidity"]
    assert got["profile"]["area"] == pytest.approx(100.0, rel=0.05)
    assert got["summary"]["area"]["q25"] < got["summary"]["area"]["median"] < got["summary"]["area"]["q75"]


def test_a_well_says_how_much_spread_its_components_carry():
    pca = _well("P1", "A1", 100.0, 0)["well_population"]["pca"]
    ratio = pca["explained_variance_ratio"]
    assert sum(ratio) == pytest.approx(1.0, abs=1e-6)
    assert ratio == sorted(ratio, reverse=True)
    assert set(pca["loadings"]) == {"pca_1", "pca_2"}


def test_a_small_well_is_summarised_without_components():
    rng = np.random.default_rng(0)
    data = {"results": [_tile(rng, 5, 100.0, "t0")], "failures": [],
            "metadata": {"scope": {"plate": "P1", "well": "A1"}}}
    got = well_run(data, {}, enough_objects=10)["well_population"]
    assert got["n_objects"] == 5 and got["pca"] is None
    assert "area" in got["profile"]


def test_the_plate_compares_its_wells_and_flags_the_odd_one():
    wells = [_well("P1", w, 100.0, i) for i, w in enumerate(["A1", "A2", "A3", "A4", "A5"])]
    wells.append(_well("P1", "B1", 200.0, 9))
    got = plate_run({"results": wells, "failures": [],
                     "metadata": {"scope": {"plate": "P1", "well": "B1"}}}, {})["plate_summary"]
    assert got["n_wells"] == 6
    assert got["n_objects"] == 6 * 90
    assert got["scope"] == {"plate": "P1"}
    assert set(got["profiles"]) == {"A1", "A2", "A3", "A4", "A5", "B1"}
    assert got["features"]["area"]["median_well"] == pytest.approx(100.0, rel=0.05)
    assert got["robust_z"]["B1"]["area"] > 3
    assert "area" in got["outlying_wells"]["B1"]
    assert not any("area" in f for w, f in got["outlying_wells"].items() if w != "B1")


def test_both_steps_write_their_tables_when_asked(tmp_path):
    well = _well("P1", "A1", 100.0, 0, output_dir=str(tmp_path))
    written = well["well_population"]["written"]
    assert Path(written["objects"]).name == "P1_A1_objects.csv"
    assert Path(written["pca"]).exists()
    plate = plate_run({"results": [well, _well("P1", "A2", 100.0, 1)], "failures": [],
                       "metadata": {"scope": {"plate": "P1"}}}, {}, output_dir=str(tmp_path))
    assert Path(plate["plate_summary"]["written"]["profiles"]).name == "P1_well_profiles.csv"


def test_the_plate_recipe_registers_with_its_two_scopes():
    import yaml

    recipe = Path(__file__).resolve().parents[1] / "pipelines" / "object_analysis_plate.yaml"
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from engine._run import parse_yaml, split_phases

    _, steps, _ = parse_yaml(recipe)
    phases = split_phases(steps)
    assert [p.scope for p in phases] == [None, "well", "plate"]
    assert [s.name for s in phases[1].steps] == ["summarise_well"]
    assert [s.name for s in phases[2].steps] == ["summarise_plate"]
