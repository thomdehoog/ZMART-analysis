"""compare_populations -- the units inside one completed unit, side by side.

A scoped step, one level above ``summarise_population``: it runs once a
wider unit is complete -- for example a carrier, once all its compartments
are summarised -- and receives those summaries together. It compares them:

- the profiles, one row per unit and one column per feature (each value the
  unit's median over its objects);
- for every feature, the median unit and how far units spread around it
  (the median absolute deviation, MAD);
- for every unit and feature, a robust z-score: how many spreads the unit
  sits from the median unit. A unit far out on many features is worth a
  look, whether it is biology or a bubble.

These are the usual aggregation and normalisation of image-based profiling
(pycytominer's ``aggregate`` and ``normalize(method="mad_robustize")``),
done here with pandas and numpy.

Takes ``pipeline_data["results"]``, one ``summarise_population`` result per
unit. Publishes ``pipeline_data["comparison"]`` and, when ``output_dir`` is
given, writes the profiles and the z-scores as CSV files.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from shared.population import MAD_TO_SD, robust_z  # noqa: E402
from summarise_population import unit_name  # noqa: E402

METADATA = {
    "description": "Compare the populations of the units inside one completed unit",
    "version": "1.0",
    "max_workers": 1,
    "environment": "ZMART--object_analysis--classical",
}


def run(pipeline_data: dict, state: dict, **params) -> dict:
    import pandas as pd

    meta = pipeline_data.get("metadata", {})
    level = meta.get("scope_level")
    own = meta.get("unit", meta.get("scope", {})) or {}
    units = [r["population"] for r in pipeline_data.get("results", [])
             if "population" in r]
    compared = units[0]["level"] if units else None
    # A unit below this one that failed, and tiles whose unit was never
    # closed when this one was; the engine reports both as failures.
    failures = pipeline_data.get("failures", [])
    n_failed_units = sum(f.get("step") != "engine" for f in failures)
    n_not_closed = sum(f.get("step") == "engine" for f in failures)

    names = [_short_name(u["scope"], compared) for u in units]
    profiles = pd.DataFrame([u["profile"] for u in units], index=names).sort_index()
    # Only features every unit measured can be compared.
    profiles = profiles.dropna(axis=1, how="any")

    features = {}
    zscores = pd.DataFrame(index=profiles.index)
    for name in profiles.columns:
        values = profiles[name].to_numpy(dtype=np.float64)
        median = float(np.median(values))
        mad = float(np.median(np.abs(values - median)) * MAD_TO_SD)
        features[name] = {"median_unit": median, "mad": mad}
        zscores[name] = robust_z(values)

    outlying = int(params.get("outlier_z", 3))
    flagged = {
        unit: sorted(zscores.columns[np.abs(zscores.loc[unit].to_numpy()) > outlying].tolist())
        for unit in zscores.index
    }
    flagged = {unit: found for unit, found in flagged.items() if found}

    written = {}
    output_dir = params.get("output_dir")
    if output_dir and len(profiles):
        folder = Path(output_dir)
        folder.mkdir(parents=True, exist_ok=True)
        stem = f"{unit_name(own)}_{compared or 'unit'}"
        written = {
            "profiles": str(folder / f"{stem}_profiles.csv"),
            "robust_z": str(folder / f"{stem}_robust_z.csv"),
        }
        profiles.to_csv(written["profiles"], index_label=compared or "unit")
        zscores.to_csv(written["robust_z"], index_label=compared or "unit")

    return {
        "metadata": meta,
        "comparison": {
            "level": level,
            "scope": own,
            "compared": compared,
            "n_units": len(units),
            "n_failed_units": n_failed_units,
            "n_not_closed": n_not_closed,
            "n_objects": int(sum(u["n_objects"] for u in units)),
            "objects_per_unit": {n: u["n_objects"] for n, u in zip(names, units, strict=True)},
            "features": features,
            "profiles": {unit: row.to_dict() for unit, row in profiles.iterrows()},
            "robust_z": {unit: row.to_dict() for unit, row in zscores.iterrows()},
            "outlying": flagged,
            "outlier_z": outlying,
            "written": written,
        },
    }


def _short_name(scope: dict, level: str | None) -> str:
    """A unit's name among its siblings: its own number at its own level."""
    if level in scope:
        return str(scope[level])
    return unit_name(scope)
