"""summarise_population -- the objects of one completed unit, as a population.

A scoped step. It runs once a unit of the sample is complete -- a group (a
tile set), a compartment, or a carrier, whichever scope the recipe gives it
-- and receives the object tables of every tile in that unit. It joins them
into one population and describes it:

- how many tiles and objects the unit has, and how many tiles failed;
- for every measured feature, the median, quartiles, mean and spread;
- the unit's profile: the median of each feature over its objects, which is
  what ``compare_populations`` works on one level up;
- the first principal components of the population, with the share of the
  spread each carries and the features that pull on them.

Nothing here knows what the unit is. A compartment may be a well of a plate,
a region of a slide, or a stretch of a cleared sample; it is a number
either way.

The conditioning and the PCA are those of ``workflows/_population.py``, the
same the on-demand population plots use.

Takes ``pipeline_data["results"]``, one tile result each, as the engine hands
a scoped step. Publishes ``pipeline_data["population"]`` and, when
``output_dir`` is given, writes the joined object table and its principal
components as CSV files named after the unit, for example
``carrier1_compartment3_objects.csv``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _population import (  # noqa: E402
    conditioned,
    feature_summary,
    measured_columns,
    principal_components,
)

METADATA = {
    "description": "Summarise the object population of one completed unit",
    "version": "1.0",
    "max_workers": 4,
    "environment": "ZMART--object_analysis--classical",
}


def run(pipeline_data: dict, state: dict, **params) -> dict:
    import pandas as pd

    meta = pipeline_data.get("metadata", {})
    level = meta.get("scope_level")
    scope = _unit_of(meta.get("scope", {}) or {}, level)
    tiles = pipeline_data.get("results", [])
    enough_measured = float(params.get("enough_measured", 0.5))
    enough_objects = int(params.get("enough_objects", 10))
    seed = int(params.get("seed", 0))

    frames = []
    for tile in tiles:
        props = tile.get("object_analysis", {}).get("objects", {}).get("properties")
        if props:
            frames.append(pd.DataFrame(props))
    population = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    features = measured_columns(population, enough_measured) if len(population) else []
    summary = feature_summary(population, features)
    profile = {name: stats["median"] for name, stats in summary.items()}

    pca = None
    components = None
    if len(population) >= enough_objects and len(features) >= 2:
        components, explained, loadings = principal_components(
            conditioned(population, features), features, seed=seed,
            n_components=int(params.get("components", 10)),
        )
        pca = {"explained_variance_ratio": explained, "loadings": loadings}

    written = {}
    output_dir = params.get("output_dir")
    if output_dir and len(population):
        written = _write(Path(output_dir), scope, population, components)

    return {
        "metadata": meta,
        "population": {
            "level": level,
            "scope": scope,
            "n_tiles": len(tiles),
            "n_failed_tiles": len(pipeline_data.get("failures", [])),
            "n_objects": int(len(population)),
            "features": features,
            "summary": summary,
            "profile": profile,
            "pca": pca,
            "written": written,
        },
    }


#: The levels a sample is divided into, widest first. The tile is the
#: narrowest and needs no scope: every per-tile step already runs on one.
LEVELS = ("carrier", "compartment", "group")


def _unit_of(scope: dict, level: str | None) -> dict:
    """The part of a submit's scope that names this unit: its own level and
    every wider one. A tile's group says nothing about its compartment's
    population, so narrower levels are left out."""
    if level not in LEVELS:
        return dict(scope)
    wider = LEVELS[: LEVELS.index(level) + 1]
    return {k: scope[k] for k in wider if k in scope}


def unit_name(scope: dict) -> str:
    """A file-name stem for a unit, such as ``carrier1_compartment3``."""
    return "_".join(f"{k}{scope[k]}" for k in LEVELS if k in scope) or "all"


def _write(folder: Path, scope: dict, population, components) -> dict:
    """The joined table, and its first two components, as CSV files."""
    folder.mkdir(parents=True, exist_ok=True)
    stem = unit_name(scope)
    written = {"objects": folder / f"{stem}_objects.csv"}
    population.to_csv(written["objects"], index=False)
    if components is not None:
        written["pca"] = folder / f"{stem}_pca.csv"
        frame = population[["object_id"]].copy() if "object_id" in population else population.iloc[:, :0].copy()
        frame["pca_1"], frame["pca_2"] = components[:, 0], components[:, 1]
        frame.to_csv(written["pca"], index=False)
    return {key: str(path) for key, path in written.items()}
