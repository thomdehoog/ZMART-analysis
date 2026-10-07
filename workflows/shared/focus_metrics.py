"""focus_metrics -- the sharpness measures every focus-related step shares.

Each measure takes one 2-D plane as float64 and returns one number that is
larger when the plane is sharper. ``score_focus`` scores a whole z-stack with
all of them; ``measure_objective_pair`` uses Brenner alone to find the sharp
height of a calibration stack. One copy here, so the two cannot drift apart.

Which family suits which sample, in short:

- gradient (``brenner``): sharp peak on structured tissue; noise in a dim
  image can look like sharpness.
- frequency (``dct``): entropy of the spectrum, with the constant (DC) term
  left out so a brighter background does not change the answer.
- autocorrelation (``vollath_f4``): strongest where Brenner is weakest, in
  dim and noisy fluorescence; near zero on pure noise, by design.
- intensity (``intensity``): confocal only, where the pinhole makes
  brightness itself peak at the focal plane.

Pertuz, Puig and Garcia (2013), "Analysis of focus measure operators for
shape-from-focus", Pattern Recognition 46:1415, compare these families and
are the reference to cite for the choice.
"""

from __future__ import annotations

import numpy as np
from scipy.fft import dctn


def brenner(plane: np.ndarray) -> float:
    """Mean squared difference between pixels two apart, along both axes.

    Brenner (1976) used one axis and a threshold on the difference; this is
    the unthresholded form summed over both axes, because a single-axis
    Brenner is blind to structure running along it and detail in tissue has
    no preferred direction. Pertuz et al. (2013) call the max-of-two-axes
    form "BREN"; this sums instead, which ranks planes the same way.
    """
    across = plane[:, 2:] - plane[:, :-2]
    down = plane[2:, :] - plane[:-2, :]
    return float(np.mean(across * across) + np.mean(down * down))


def dct_entropy(plane: np.ndarray) -> float:
    """Shannon entropy, in bits, of the normalised DCT energy without the DC term.

    The (0, 0) coefficient is the plane's mean brightness squared and carries
    no sharpness at all; left in, it dominates the energy so completely that
    the entropy collapses towards zero on any plane with a bright background
    and the metric ends up measuring contrast over mean, not spectral
    content. Setting it to zero first is what makes the measure independent
    of brightness, as the DCT-based measures in Pertuz et al. (2013) are.
    """
    energy = np.square(dctn(plane, norm="ortho"))
    energy[0, 0] = 0.0
    total = energy.sum()
    if total <= 0:
        return 0.0
    share = (energy / total).ravel()
    share = share[share > 0]
    return float(-np.sum(share * np.log2(share)))


def vollath_f4(plane: np.ndarray) -> float:
    """Vollath's F4 autocorrelation measure, along both axes, per pixel.

    F4 = mean(I(x) * I(x+1)) - mean(I(x) * I(x+2)), both means over the same
    pixels (Vollath 1987; "VOLA4" in Pertuz et al. 2013). A sharp image is
    correlated with its immediate neighbour and less with the one beyond;
    blur makes the two correlations alike and the difference shrinks. Noise
    that is uncorrelated from pixel to pixel cancels in the subtraction, so
    F4 holds up in dim, noisy fluorescence where a gradient measure rewards
    the noise. The score still scales with the square of the brightness, so
    a bleaching stack should be swept in one direction only.
    """
    across = np.mean(plane[:, :-2] * plane[:, 1:-1]) - np.mean(plane[:, :-2] * plane[:, 2:])
    down = np.mean(plane[:-2, :] * plane[1:-1, :]) - np.mean(plane[:-2, :] * plane[2:, :])
    return float(across + down)


def intensity(plane: np.ndarray, percentile: float) -> float:
    """Mean of the pixels at or above the given percentile of brightness.

    Not the plain mean, so a flat background does not dilute the signal, and
    not the single maximum, so one speck of debris or a hot pixel cannot win
    on its own. The percentile is per plane, so the brightest tail of each
    plane is compared like with like.
    """
    threshold = np.percentile(plane, percentile)
    return float(np.mean(plane[plane >= threshold]))

#: The metrics on offer, by the name the YAML uses. Adding one is adding an
#: entry here; every entry is scored on every run.
METRICS = {"brenner": brenner, "dct": dct_entropy, "vollath_f4": vollath_f4}

#: Default for ``intensity_percentile``: the brightest one percent of pixels.
DEFAULT_INTENSITY_PERCENTILE = 99.0
