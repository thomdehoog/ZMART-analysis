# `driver_configuration` workflow

Measures two things that a
[ZMART driver](https://github.com/thomdehoog/ZMART-drivers) must know before
it can steer the stage by what it sees in the picture. Both are measured
from pictures alone, so the same steps work on every microscope.

| Pipeline | Step | What it measures |
|---|---|---|
| `orientation.yaml` | `measure_orientation` | How the camera is turned relative to the stage, and how much specimen one pixel covers. |
| `objective_pair.yaml` | `measure_objective_pair` | How far apart two objectives look and focus. |

You rarely submit these yourself. The setup notebooks in ZMART Microscopy
run both pipelines for you. The docstring of each step, in `steps/`, says
exactly what goes in and what comes back.

## Orientation

A camera is often mounted a quarter turn or a half turn away from the
stage's own X and Y. Some settings mirror the image as well.

The step takes three pictures of the same field:

1. one at the starting position,
2. one after the stage moved a known distance along +X,
3. one after the same move along +Y.

The direction in which the features moved tells how the picture is turned.
The distance they moved tells how much specimen one pixel covers.

## Objective pair

A target is often found with a low-power overview lens and then imaged
again with a high-power one. The two lenses do not look at exactly the same
place, and they do not focus at exactly the same height.

The step measures both offsets. It needs the same field seen through each
lens, and a short focus stack through each.

## Review pictures

Both steps write a review picture. Use it to check the measurement against
what the pictures show.

## Environment

Both steps run in `ZMART--driver_configuration--main`. It holds numpy,
scipy and scikit-image for the measurements, matplotlib for the review
pictures, and the image readers. It needs no PyTorch and no GPU. Create it
once with:

```bash
python workflows/driver_configuration/environments/setup_env.py
```
