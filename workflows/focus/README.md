# `focus` workflow

Finds the sharp height in a z-stack. Every plane gets a sharpness score, and
the height where the score peaks is reported as the focus. This is how the
microscope finds the tissue before it images a position.

```text
focus.yaml:   score_focus   every plane scored, the peak refined between planes
```

The recipe is [`pipelines/focus.yaml`](pipelines/focus.yaml). The step's
docstring, in [`steps/score_focus.py`](steps/score_focus.py), lists exactly
what it takes and what it returns.

## Choosing a sharpness measure

All four measures are scored on every stack, so their curves can be compared
afterwards. `metric` in `focus.yaml` says which one decides the height.

| Measure | Family | Good at | Watch out for |
| --- | --- | --- | --- |
| `brenner` (default) | Gradient | A sharp, clear peak on structured tissue | Noise in a dim image looks like sharpness |
| `dct` | Frequency | Does not care how bright the image is | A broader peak, and slower to compute |
| `vollath_f4` | Autocorrelation | Dim, noisy fluorescence, exactly where Brenner is weakest | A slightly flatter peak than Brenner |
| `intensity` | Brightness | Sparse or faint samples on a confocal | Only works on a confocal; bleaching and bright debris can mislead it |

A good default is `brenner`. If your sample is dim and noisy, try
`vollath_f4`. On a confocal with a sparse sample, try `intensity`.

Pertuz, Puig and Garcia (2013, *Pattern Recognition* 46:1415) compare these
families on real stacks. It is the paper to cite for the choice.

### Why brightness only works on a confocal

On a widefield microscope, out-of-focus light still reaches the camera, so the
total brightness barely changes as you move through focus. On a confocal such
as the Stellaris, the pinhole blocks out-of-focus light, so the image is
brightest at the focal plane. `intensity` uses the mean of the brightest 1 %
of pixels (set by `intensity_percentile`) rather than the plain mean. That way
a single bright speck cannot take over, and a large dark background does not
dilute the signal.

Bleaching makes each plane a little dimmer than the one before it. Sweep the
stack in one direction only, bottom to top, so the effect always pushes the
same way. With strong bleaching the brightness peak shifts towards the planes
taken first. The other three measures shift less, because they measure
contrast rather than brightness.

## The ends of a stack

The first and last planes of a z-drive often carry artefacts: the stage has
not settled yet, or the image has a hard edge. Every sharpness measure
rewards hard edges, so an artefact can look like perfect focus. `skip_ends`
(2 by default) stops that many planes at each end from winning. They are
still scored and returned, so you can see them in the curve.

## Environment

The step runs in `ZMART--focus--main`: numpy, scipy for the DCT, and the image
readers. It needs no torch and no GPU. Create it once with:

```bash
python workflows/focus/environments/setup_env.py
```

## Other options we looked at

- **Tenengrad and variance of the Laplacian** are other gradient measures.
  They behave much like Brenner, so they add little.
- **Wavelet energy** is another frequency measure. It behaves much like `dct`.
- **The Leica's reflection autofocus** bounces light off the coverslip. It is
  fast and needs no image, but it finds the glass, not the tissue, so it
  needs an offset on top.

Check any new measure against a real confocal stack from the rig before
relying on it.
