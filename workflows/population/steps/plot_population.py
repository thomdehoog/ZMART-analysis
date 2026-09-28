"""plot_population -- a multidimensional plot of a discovered population.

The features object analysis measures are made for gating one pair at a
time; this folds all of them into two axes so the population's own
structure can be gated on too -- objects that are alike stand together,
whatever combination of columns makes them alike:

- ``pca``, the first two principal components (``pca_1``, ``pca_2``): linear,
  and a fraction of a second over half a million objects;
- ``umap``, a UMAP layout (``umap_1``, ``umap_2``) of the first 50 of those
  components: minutes over half a million, which is why the operator asks
  for it and detection never runs it.

Takes ``input["table"]``, the population table written when a whole overview
has been detected (one row an object, one column a feature), narrowed to
``input["ids"]`` when given, and ``input["kind"]``. Writes the two columns
beside the table (``<stem>_pca.csv``, ``<stem>_umap.csv``); a UMAP writes the
components too, since it stands on them.

What goes in is chosen, not everything numeric: identity and place stay out
(``label``, ``bbox_*``, ``centroid_*``, the stage position), and so does
``bg_global_mean*`` -- one number per field, which would lay out the field an
object came from rather than the object. A column measured for fewer than
half the objects stays out; the odd missing value takes its column's
median. Each column is centred on its median and scaled by its spread
between the quartiles, so a few bright outliers cannot own an axis. The
seed is pinned: the same population always gets the same plot.

Publishes under ``pipeline_data["plot_population"]``::

    written                   {kind: path} for every file written
    objects                   how many objects were plotted
    features                  the columns the plot stands on
    explained_variance_ratio  the share of the spread each component carries,
                              so the two axes drawn can be read honestly
    loadings                  the five features pulling hardest on each of
                              the two drawn components, with their weights
    umap                      the neighbours, min_dist, seed and number of
                              components a UMAP was laid out from, or None
"""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

METADATA = {
    "description": "Principal components or a UMAP of a discovered population",
    "version": "1.0",
    "max_workers": 1,
    "environment": "ZMART--population--main",
}

#: The two plots there are, and the columns each lands as.
KINDS = {"pca": ("pca_1", "pca_2"), "umap": ("umap_1", "umap_2")}

#: Columns of the population table that are not measurements of the object.
NOT_MEASURED = {"field", "position_label", "id", "x_um", "y_um", "intensity", "r", "label"}
NOT_MEASURED_PREFIXES = ("bbox_", "centroid_", "weighted_centroid", "stage_", "bg_global_mean")


def run(pipeline_data: dict, state: dict, **params) -> dict:
    verbose = pipeline_data.get("metadata", {}).get("verbose", 0)
    inp = pipeline_data["input"]
    kind = inp.get("kind")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {tuple(KINDS)}, got {kind!r}.")
    table = Path(inp["table"])
    enough_objects = int(params.get("enough_objects", 10))
    matrix, ids, features = _conditioned(
        _read_population(table, inp.get("ids")), float(params.get("enough_measured", 0.5)),
    )
    if len(ids) < enough_objects:
        raise ValueError(
            f"only {len(ids)} objects; a plot needs at least {enough_objects} "
            "to say anything about the population"
        )
    if not features:
        raise ValueError("no feature was measured widely enough to plot the objects on")
    seed = int(params.get("seed", 0))

    from sklearn.decomposition import PCA

    room = min(int(params.get("components", 50)), matrix.shape[0], matrix.shape[1])
    pca = PCA(n_components=room, random_state=seed)
    components = pca.fit_transform(matrix)
    explained = [float(v) for v in pca.explained_variance_ratio_]
    # The two axes an operator sees are only as honest as the share of the
    # spread they carry, and which features pull on them. Both go out with
    # the plot so the picture can be read rather than trusted.
    loadings = {
        f"pca_{axis + 1}": {
            feature: float(weight)
            for feature, weight in sorted(
                zip(features, pca.components_[axis]), key=lambda fw: -abs(fw[1])
            )[:5]
        }
        for axis in range(min(2, components.shape[1]))
    }
    if components.shape[1] < 2:
        components = np.hstack([components, np.zeros((components.shape[0], 1))])
    stem = table.name.removesuffix("_objects.csv")
    written = {"pca": _write(table.with_name(f"{stem}_pca.csv"), ids, KINDS["pca"], components[:, :2])}
    umap_settings = None
    if kind == "umap":
        from umap import UMAP

        umap_settings = {
            "n_neighbors": min(int(params.get("neighbours", 15)), len(ids) - 1),
            "min_dist": float(params.get("min_dist", 0.1)),
            "components_in": int(components.shape[1]),
            "random_state": seed,
        }
        laid_out = UMAP(
            n_components=2, n_neighbors=umap_settings["n_neighbors"],
            min_dist=umap_settings["min_dist"], random_state=seed,
        ).fit_transform(components)
        written["umap"] = _write(table.with_name(f"{stem}_umap.csv"), ids, KINDS["umap"], laid_out)
    if verbose:
        print(f"  [plot_population] {kind} over {len(ids)} objects, {len(features)} features")
    pipeline_data["plot_population"] = {
        "written": {key: str(path) for key, path in written.items()},
        "objects": len(ids),
        "features": features,
        "explained_variance_ratio": explained,
        "loadings": loadings,
        "umap": umap_settings,
    }
    return pipeline_data


def _read_population(table: Path, ids):
    """The population table, narrowed to *ids* when given, in its own order."""
    import pandas as pd

    frame = pd.read_csv(table, dtype={"id": str, "position_label": str})
    if ids is not None:
        frame = frame[frame["id"].isin(set(map(str, ids)))]
    return frame.reset_index(drop=True)


def _measured(name: str) -> bool:
    return name not in NOT_MEASURED and not name.startswith(NOT_MEASURED_PREFIXES)


def _conditioned(frame, enough_measured: float) -> tuple[np.ndarray, list[str], list[str]]:
    """``(matrix, ids, features)``: one row an object in the table's order,
    one column a measurement worth keeping, each centred on its median and
    scaled by its interquartile spread.

    The centring and scaling are scikit-learn's ``RobustScaler`` and the
    missing values its ``SimpleImputer`` with the median, so the conditioning
    is the documented, citable one rather than a private variant. Two rules
    are ours: a column measured for fewer than ``enough_measured`` of the
    objects stays out, and a column with the same number for every object is
    dropped, because it says nothing about any of them.
    """
    from sklearn.impute import SimpleImputer
    from sklearn.preprocessing import RobustScaler

    ids = [str(one) for one in frame["id"]]
    kept = []
    for name in sorted(name for name in frame.columns if _measured(name)):
        column = np.asarray(frame[name], dtype=np.float64)
        finite = np.isfinite(column)
        if not finite.size or finite.mean() < enough_measured:
            continue
        quarter, three_quarters = np.percentile(column[finite], [25, 75])
        if three_quarters - quarter == 0.0 and column[finite].std() == 0.0:
            continue  # the same number for every object says nothing
        kept.append(name)
    if not kept:
        return np.empty((len(ids), 0)), ids, kept
    raw = np.asarray(frame[kept], dtype=np.float64)
    raw[~np.isfinite(raw)] = np.nan
    filled = SimpleImputer(strategy="median").fit_transform(raw)
    # RobustScaler leaves a zero interquartile range unscaled; for such a
    # column (rare, but a feature that is constant for three quarters of the
    # objects does it) fall back to the standard deviation, as before.
    scaled = RobustScaler(quantile_range=(25.0, 75.0)).fit_transform(filled)
    q1, q3 = np.percentile(filled, [25, 75], axis=0)
    flat = (q3 - q1) == 0.0
    if flat.any():
        std = filled[:, flat].std(axis=0)
        scaled[:, flat] = (filled[:, flat] - np.median(filled[:, flat], axis=0)) / std
    return scaled, ids, kept


def _write(path: Path, ids: list[str], columns: tuple[str, str], values: np.ndarray) -> Path:
    with path.open("w", encoding="utf-8", newline="") as out:
        rows = csv.writer(out)
        rows.writerow(["id", *columns])
        for an_id, (a, b) in zip(ids, values, strict=True):
            rows.writerow([an_id, float(a), float(b)])
    return path
