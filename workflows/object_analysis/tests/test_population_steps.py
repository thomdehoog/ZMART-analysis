"""summarise_population and compare_populations: what a unit reports.

The tiles are object tables as build_object_table publishes them, made up
here so the numbers are known: five compartments of ordinary cells and one
whose cells are twice as large, so the carrier comparison must single it
out. The units are plain numbers, as in the capture label.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("pandas")
pytest.importorskip("sklearn")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "steps"))
from compare_populations import run as compare_run  # noqa: E402
from summarise_population import run as summarise_run  # noqa: E402


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


def _compartment(carrier, compartment, area_mean, seed, **params):
    rng = np.random.default_rng(seed)
    tiles = [_tile(rng, 30, area_mean, f"M{compartment}_P{t}") for t in range(3)]
    data = {
        "results": tiles,
        "failures": [],
        "metadata": {
            "scope_level": "compartment",
            # The submit that closed the compartment also named its group;
            # a compartment's population is not the group's, so it is dropped.
            "scope": {"carrier": carrier, "compartment": compartment, "group": 7},
        },
    }
    return summarise_run(data, {}, **params)


def _carrier(results, carrier=1, **params):
    data = {
        "results": results,
        "failures": [],
        "metadata": {"scope_level": "carrier",
                     "scope": {"carrier": carrier, "compartment": 6}},
    }
    return compare_run(data, {}, **params)["comparison"]


def test_a_compartment_joins_its_tiles_into_one_population():
    got = _compartment(1, 3, 100.0, 0)["population"]
    assert got["level"] == "compartment"
    assert got["scope"] == {"carrier": 1, "compartment": 3}
    assert got["n_tiles"] == 3
    assert got["n_objects"] == 90
    assert got["features"] == ["area", "eccentricity", "intensity_mean", "solidity"]
    assert got["profile"]["area"] == pytest.approx(100.0, rel=0.05)
    area = got["summary"]["area"]
    assert area["q25"] < area["median"] < area["q75"]


def test_a_population_says_how_much_spread_its_components_carry():
    pca = _compartment(1, 3, 100.0, 0)["population"]["pca"]
    ratio = pca["explained_variance_ratio"]
    assert sum(ratio) == pytest.approx(1.0, abs=1e-6)
    assert ratio == sorted(ratio, reverse=True)
    assert set(pca["loadings"]) == {"pca_1", "pca_2"}


def test_a_small_unit_is_summarised_without_components():
    rng = np.random.default_rng(0)
    data = {"results": [_tile(rng, 5, 100.0, "t0")], "failures": [],
            "metadata": {"scope_level": "group",
                         "scope": {"carrier": 1, "compartment": 3, "group": 2}}}
    got = summarise_run(data, {}, enough_objects=10)["population"]
    assert got["scope"] == {"carrier": 1, "compartment": 3, "group": 2}
    assert got["n_objects"] == 5 and got["pca"] is None
    assert "area" in got["profile"]


def test_the_carrier_compares_its_compartments_and_flags_the_odd_one():
    units = [_compartment(1, m, 100.0, m) for m in range(1, 6)]
    units.append(_compartment(1, 6, 200.0, 9))
    got = _carrier(units)
    assert got["level"] == "carrier"
    assert got["compared"] == "compartment"
    assert got["scope"] == {"carrier": 1}
    assert got["n_units"] == 6
    assert got["n_objects"] == 6 * 90
    assert set(got["profiles"]) == {"1", "2", "3", "4", "5", "6"}
    assert got["features"]["area"]["median_unit"] == pytest.approx(100.0, rel=0.05)
    assert got["robust_z"]["6"]["area"] > 3
    assert "area" in got["outlying"]["6"]
    assert not any("area" in f for unit, f in got["outlying"].items() if unit != "6")


def test_both_steps_write_their_tables_when_asked(tmp_path):
    one = _compartment(1, 3, 100.0, 0, output_dir=str(tmp_path))
    written = one["population"]["written"]
    assert Path(written["objects"]).name == "carrier1_compartment3_objects.csv"
    assert Path(written["pca"]).exists()
    got = _carrier([one, _compartment(1, 4, 100.0, 1)], output_dir=str(tmp_path))
    assert Path(got["written"]["profiles"]).name == "carrier1_compartment_profiles.csv"


def test_the_scoped_recipe_registers_with_its_two_scopes():
    recipe = Path(__file__).resolve().parents[1] / "pipelines" / "object_analysis_scoped.yaml"
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from engine.pipeline import parse_yaml, split_phases

    _, steps, _ = parse_yaml(recipe)
    phases = split_phases(steps)
    assert [p.scope for p in phases] == [None, "compartment", "carrier"]
    assert [s.name for s in phases[1].steps] == ["summarise_population"]
    assert [s.name for s in phases[2].steps] == ["compare_populations"]
