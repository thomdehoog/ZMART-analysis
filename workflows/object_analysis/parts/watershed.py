"""The fast detector: nuclei found by a watershed, with no model to load.

This is a port of QuPath's cell detection (``WatershedCellDetection``,
Bankhead et al. 2017). It answers in a second or two per field on an
ordinary processor, which makes it the choice when the microscope is
waiting and a minute of Cellpose would be too long.
"""

from __future__ import annotations

import numpy as np

from .masks import filter_masks_by_area


def watershed_masks(plane, *, diameter_px: float, threshold: float):
    """Nuclei in one plane, the way QuPath's cell detection finds them.

    The steps, in order: the background is estimated by opening by
    reconstruction and taken off; a Gaussian blur and a 3 x 3 Laplacian make
    the blob response; where the response is positive is the foreground and
    its regional maxima seed a watershed on it; regions whose mean on the
    background-subtracted plane is under ``threshold`` (in counts) are
    dropped; what is left is filled and split again on its distance
    transform, so touching nuclei part; and objects outside the area range
    go. QuPath's micrometre defaults (8 um background radius, 1.5 um sigma,
    10 and 400 um^2 areas, for nuclei of about 8 to 10 um) are taken as
    ratios of the one size the operator gives: background radius D, sigma
    D / 8, areas A / 8 and 5 A with A the disc of diameter D.

    Only scipy and scikit-image are needed, imported here so a Cellpose run
    never pays for them. Returns the int32 label image and the parameters
    as used, in pixels.
    """
    from scipy import ndimage
    from skimage import filters, measure, morphology, segmentation

    f = np.asarray(plane, dtype=np.float32)
    d = max(float(diameter_px), 2.0)
    radius = max(int(round(d)), 1)
    sigma = max(d / 8.0, 0.5)
    disc = np.pi * (d / 2.0) ** 2
    min_area = max(int(round(disc / 8.0)), 1)
    max_area = max(int(round(disc * 5.0)), min_area + 1)
    used = {
        "threshold": float(threshold),
        "sigma_px": float(sigma),
        "background_radius_px": int(radius),
        "min_area_px": int(min_area),
        "max_area_px": int(max_area),
        "split_by_shape": True,
    }

    eroded = morphology.erosion(f, morphology.disk(radius, decomposition="sequence"))
    background = morphology.reconstruction(eroded, f, method="dilation")
    subtracted = f - background
    blurred = filters.gaussian(subtracted, sigma=sigma, preserve_range=True)
    response = ndimage.convolve(
        blurred, np.array([[0, -1, 0], [-1, 4, -1], [0, -1, 0]], dtype=np.float32)
    )
    above = response > 0
    seeds = measure.label(morphology.local_maxima(response) & above, connectivity=1)
    regions = segmentation.watershed(-response, seeds, mask=above, connectivity=1)
    labels = np.arange(1, int(regions.max()) + 1)
    if labels.size == 0:
        return np.zeros(f.shape, dtype=np.int32), used
    means = np.asarray(ndimage.mean(subtracted, regions, labels))
    kept = np.isin(regions, labels[means > threshold])
    kept = ndimage.binary_dilation(kept, structure=np.ones((3, 3), dtype=bool)) & above
    kept = ndimage.binary_fill_holes(kept)
    distance = ndimage.distance_transform_edt(kept)
    peaks = measure.label(morphology.h_maxima(distance, 0.5) & kept, connectivity=1)
    split = segmentation.watershed(-distance, peaks, mask=kept, watershed_line=True)
    objects = np.arange(1, int(split.max()) + 1)
    if objects.size:
        means = np.asarray(ndimage.mean(subtracted, split, objects))
        split = np.where(np.isin(split, objects[means >= threshold]), split, 0)
    masks, _ = filter_masks_by_area(
        np.asarray(split, dtype=np.int32), min_area_px=min_area, max_area_px=max_area
    )
    # These are the detector's own labels, so they are numbered 1..n here,
    # once. The filters the operator applies afterwards keep these numbers.
    masks, _, _ = segmentation.relabel_sequential(np.asarray(masks, dtype=np.int32))
    return np.asarray(masks, dtype=np.int32), used
