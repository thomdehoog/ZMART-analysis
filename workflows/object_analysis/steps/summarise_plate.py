"""summarise_plate -- one summary of a plate, once every well is done.

A scoped step: it runs once per plate, after every well of that plate has
been summarised, and it receives the well summaries together. It lays them
side by side and compares them:

- the plate's well profiles, one row per well and one column per feature
  (each value the well's median over its objects);
- for every feature, the plate's median well and how far wells spread
  around it (the median absolute deviation, MAD);
- for every well and feature, a robust z-score: how many spreads the well
  sits from the plate's median well. A well far out on many features is
  worth a look, whether it is biology or a bubble.

These are the usual well-level aggregation and plate normalisation of
image-based profiling (pycytominer's ``aggregate`` and
``normalize(method="mad_robustize")``), done here with pandas and numpy.

Takes ``pipeline_data["results"]``, one ``summarise_well`` result per well.
Publishes ``pipeline_data["plate_summary"]`` and, when ``output_dir`` is
given, writes the profiles and the z-scores as CSV files.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _population import MAD_TO_SD, robust_z  # noqa: E402

METADATA = {
    "description": "Compare the wells of one complete plate",
    "version": "1.0",
    "max_workers": 1,
    "environment": "ZMART--object_analysis--classical",
}


def run(pipeline_data: dict, state: dict, **params) -> dict:
    import pandas as pd

    meta = pipeline_data.get("metadata", {})
    scope = meta.get("scope", {}) or {}
    wells = [r["well_population"] for r in pipeline_data.get("results", [])
             if "well_population" in r]

    names = [_well_name(w["scope"]) for w in wells]
    profiles = pd.DataFrame([w["profile"] for w in wells], index=names).sort_index()
    # Only features every well measured can be compared across the plate.
    profiles = profiles.dropna(axis=1, how="any")

    features = {}
    zscores = pd.DataFrame(index=profiles.index)
    for name in profiles.columns:
        values = profiles[name].to_numpy(dtype=np.float64)
        median = float(np.median(values))
        mad = float(np.median(np.abs(values - median)) * MAD_TO_SD)
        features[name] = {"median_well": median, "mad": mad}
        zscores[name] = robust_z(values)

    outlying = int(params.get("outlier_z", 3))
    flagged = {
        well: sorted(zscores.columns[np.abs(zscores.loc[well].to_numpy()) > outlying].tolist())
        for well in zscores.index
    }
    flagged = {well: names for well, names in flagged.items() if names}

    written = {}
    output_dir = params.get("output_dir")
    if output_dir and len(profiles):
        folder = Path(output_dir)
        folder.mkdir(parents=True, exist_ok=True)
        stem = str(scope.get("plate", "plate"))
        written = {
            "profiles": str(folder / f"{stem}_well_profiles.csv"),
            "robust_z": str(folder / f"{stem}_well_robust_z.csv"),
        }
        profiles.to_csv(written["profiles"], index_label="well")
        zscores.to_csv(written["robust_z"], index_label="well")

    return {
        "metadata": meta,
        "plate_summary": {
            "scope": {k: v for k, v in scope.items() if k != "well"},
            "n_wells": len(wells),
            "n_objects": int(sum(w["n_objects"] for w in wells)),
            "objects_per_well": {n: w["n_objects"] for n, w in zip(names, wells)},
            "features": features,
            "profiles": {well: row.to_dict() for well, row in profiles.iterrows()},
            "robust_z": {well: row.to_dict() for well, row in zscores.iterrows()},
            "outlying_wells": flagged,
            "outlier_z": outlying,
            "written": written,
        },
    }


def _well_name(scope: dict) -> str:
    return str(scope.get("well", "?"))
