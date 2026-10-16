# `alignment` workflow

Measures what a microscope must know before it can steer by what it sees:
where the sample is sharp, which way the picture is turned, and how two
objectives line up. Every measurement is made from pictures alone, so the same
steps work on every microscope.

| Pipeline | Steps | What it measures |
|---|---|---|
| `focus.yaml` | `read_images` → `score_focus` | How sharp each plane of a z-stack is, and the height of the sharpest. |
| `orientation.yaml` | `read_images` → `measure_orientation` | How the camera is turned relative to the stage, and how much specimen one pixel covers. |
| `registration.yaml` | `read_images` → `measure_registration` | How far apart two objectives look and focus. |

`read_images` reads every picture the input names, from a file or an array,
and the measuring step after it never opens a file. The docstring of each
step, in `steps/`, says what goes in and what comes back.

## Focus

The operator page sends one stack per focus point: the planes as files with
their heights, or one OME-Zarr position with a z axis. All four sharpness
measures are scored, so their curves can be compared; `metric` in
`focus.yaml` says which one decides.

| Measure | Good at | Watch out for |
| --- | --- | --- |
| `brenner` (default) | A sharp, clear peak on structured tissue | Noise in a dim image looks like sharpness |
| `dct` | Does not care how bright the image is | A broader peak; empty, noisy planes score high |
| `vollath_f4` | Dim, noisy fluorescence | A slightly flatter peak than Brenner |
| `intensity` | Sparse or faint samples on a confocal | Confocal only; bleaching and bright debris mislead it |

Start with `brenner`. Try `vollath_f4` on a dim, noisy sample, and
`intensity` on a sparse sample on a confocal: there the pinhole makes the
image brightest at focus, while on a widefield brightness barely changes.
Pertuz, Puig and Garcia (2013, *Pattern Recognition* 46:1415) compare these
measures.

The first and last planes of a drive often carry artefacts with hard edges,
which every measure rewards. `skip_ends` (2 by default) stops that many
planes at each end from winning; they are still scored and returned.

## Orientation

Three pictures of one field: at the starting position, after a known move
along +X, and after the same move along +Y. Where the features went tells how
the picture is turned; how far they went tells the pixel size.

## Registration

The same field seen through each objective, and optionally a short focus
stack through each. The shift between the pictures is how far apart the
lenses look; the difference between the sharp heights is how far apart they
focus.

## Environment

All three run in `ZMART--alignment--main`: numpy, scipy, scikit-image and
the image readers. No PyTorch, no GPU. Create it once with:

```bash
python workflows/alignment/environments/setup_env.py
```
