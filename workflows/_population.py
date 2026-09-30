"""_population -- how a population of objects is summarised, in one place.

Shared by the scoped well summary in object analysis and by the on-demand
population plots, so a well's principal components mean the same thing in
both. Everything here works on a pandas table with one row per object and
one column per measurement.

The choices follow common practice in image-based profiling, so they can be
cited rather than defended:

- missing values take their column's median (scikit-learn ``SimpleImputer``);
- each column is centred on its median and scaled by its interquartile
  range (scikit-learn ``RobustScaler``), so a few bright outliers cannot own
  an axis;
- a well's profile is the median of each feature over its objects, and a
  well is compared with its plate by a robust z-score, the median absolute
  deviation (MAD) scaled by 1.4826. These are the defaults of pycytominer's
  ``aggregate`` and ``normalize(method="mad_robustize")``.
"""

from __future__ import annotations

import numpy as np

#: Columns that say which object it is or where it sits, not what it is like.
NOT_MEASURED = {
    "field", "position_label", "id", "x_um", "y_um", "intensity", "r", "label",
    "object_id", "tile_name",
}
NOT_MEASURED_PREFIXES = (
    "bbox_", "centroid_", "weighted_centroid", "stage_", "bg_global_mean",
)

#: Scales a median absolute deviation to the standard deviation of a normal
#: distribution, so a robust z-score reads like an ordinary one.
MAD_TO_SD = 1.4826


def measured(name: str) -> bool:
    """Whether a column is a measurement of the object."""
    return name not in NOT_MEASURED and not name.startswith(NOT_MEASURED_PREFIXES)


def measured_columns(frame, enough_measured: float) -> list[str]:
    """The numeric measurement columns worth keeping, in name order.

    A column measured for fewer than ``enough_measured`` of the objects
    stays out, and so does one with the same value for every object,
    because it says nothing about any of them.
    """
    from pandas.api.types import is_numeric_dtype

    kept = []
    for name in sorted(frame.columns):
        if not measured(name) or not is_numeric_dtype(frame[name]):
            continue
        column = np.asarray(frame[name], dtype=np.float64)
        finite = np.isfinite(column)
        if not finite.size or finite.mean() < enough_measured:
            continue
        if np.ptp(column[finite]) == 0.0:
            continue
        kept.append(name)
    return kept


def conditioned(frame, features: list[str]) -> np.ndarray:
    """The feature columns, imputed with the median and robustly scaled.

    A column whose interquartile range is zero (constant for at least half
    the objects) is scaled by its standard deviation instead, because
    ``RobustScaler`` would leave it unscaled.
    """
    from sklearn.impute import SimpleImputer
    from sklearn.preprocessing import RobustScaler

    if not features:
        return np.empty((len(frame), 0))
    raw = np.asarray(frame[features], dtype=np.float64)
    raw[~np.isfinite(raw)] = np.nan
    filled = SimpleImputer(strategy="median").fit_transform(raw)
    scaled = RobustScaler(quantile_range=(25.0, 75.0)).fit_transform(filled)
    q1, q3 = np.percentile(filled, [25, 75], axis=0)
    flat = (q3 - q1) == 0.0
    if flat.any():
        std = filled[:, flat].std(axis=0)
        std[std == 0] = 1.0
        scaled[:, flat] = (filled[:, flat] - np.median(filled[:, flat], axis=0)) / std
    return scaled


def principal_components(matrix: np.ndarray, features: list[str], *, seed: int = 0,
                         n_components: int = 50):
    """PCA of a conditioned matrix: ``(components, explained, loadings)``.

    ``explained`` is the share of the spread each component carries, and
    ``loadings`` the five features pulling hardest on each of the first two
    components, so a two-axis picture can be read rather than trusted.
    """
    from sklearn.decomposition import PCA

    room = min(int(n_components), matrix.shape[0], matrix.shape[1])
    pca = PCA(n_components=room, random_state=seed)
    components = pca.fit_transform(matrix)
    explained = [float(v) for v in pca.explained_variance_ratio_]
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
    return components, explained, loadings


def feature_summary(frame, features: list[str]) -> dict[str, dict[str, float]]:
    """Median, quartiles, mean, spread and count of each feature."""
    out = {}
    for name in features:
        column = np.asarray(frame[name], dtype=np.float64)
        column = column[np.isfinite(column)]
        q25, median, q75 = np.percentile(column, [25, 50, 75])
        out[name] = {
            "median": float(median), "q25": float(q25), "q75": float(q75),
            "mean": float(column.mean()), "std": float(column.std()),
            "n": int(column.size),
        }
    return out


def robust_z(values: np.ndarray) -> np.ndarray:
    """(value - median) / (1.4826 * MAD); zeros where the MAD is zero."""
    values = np.asarray(values, dtype=np.float64)
    median = np.median(values)
    mad = np.median(np.abs(values - median)) * MAD_TO_SD
    if mad == 0.0:
        return np.zeros_like(values)
    return (values - median) / mad
