# `driver_configuration` workflow

Measures two things about a microscope that a driver has to know before it
can steer the stage by what it sees in the picture. Each is measured from
pictures alone, so the same steps serve every microscope.

```text
orientation.yaml:      measure_orientation     which way the picture is turned relative to the stage
objective_pair.yaml:   measure_objective_pair  how far apart two objectives look and focus
```

**Orientation.** A camera is often mounted a quarter- or half-turn away from
the stage's own X and Y, and some settings mirror the image as well. The step
takes three pictures of the same field: one at home, one after the stage
moved a known distance along +X, and one after the same move along +Y.
Where the features went says which way the picture is turned; how far they
went says how much specimen one pixel covers.

**Objective pair.** A target found under a low-power overview lens has to be
imaged again under a high-power one, and the two lenses do not look at
exactly the same place or focus at exactly the same height. The step
measures both offsets from the same field seen through each lens and a short
focus stack through each.

Both steps write a review picture so you can check the measurement against
what the pictures show. Their docstrings, in `steps/`, say exactly what goes
in and what comes back. The setup notebooks in ZMART Microscopy run these
recipes for you.

## Environment

`ZMART--driver_configuration--main`: numpy, scipy, scikit-image for the
registration, matplotlib for the review pictures, and the image readers. No
torch and no GPU. Create it once with:

```bash
python workflows/driver_configuration/environments/setup_env.py
```
