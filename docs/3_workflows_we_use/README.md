# The workflows we use

Three workflows ship with ZMART Analysis. Each is a folder under
[`workflows/`](../../workflows) with its recipes, its steps, the scripts that
build its conda environments, and a README of its own with the details. This
page says what each one is for, so you can see which to start from.

```text
workflows/
  focus/                  find the sharp height in a z-stack
  object_analysis/        detect objects, measure them, describe populations
  driver_configuration/   measure what a driver needs to know about a microscope
```

Every workflow follows the same layout, explained in
[Implement an analysis step](../2_implement_an_analysis_step/README.md). To
run one, see [Use the engine](../1_use_the_engine/README.md).

## focus

Finds the sharp height in a z-stack. Every plane gets a sharpness score, and
the height where the score peaks is reported as the focus. This is how the
microscope finds the tissue before it images a position.

```text
focus.yaml:   score_focus   every plane scored, the peak refined between planes
```

Four sharpness measures are scored on every stack, so their curves can be
compared afterwards; `metric` in the recipe says which one decides the
height. The default, `brenner`, gives a sharp peak on structured tissue;
`vollath_f4` holds up better on dim, noisy fluorescence; `dct` does not care
how bright the image is; `intensity` is for sparse samples on a confocal.

One step, one environment, no GPU. This is the workflow to try first.

→ [`workflows/focus/README.md`](../../workflows/focus/README.md)

## object_analysis

Object-centred analysis of one image: find the objects, measure each one,
and publish a table with one row per object. Detection is Cellpose on the
GPU, or a faster classical method without one. Then, over the scopes an
acquisition completes, the objects of a compartment are described as a
population and the compartments of a carrier are laid side by side.

```text
object_analysis.yaml:        detect_objects -> extract_classical_features -> build_object_table
object_analysis_fast.yaml:   the same, with classical detection and fewer texture features
object_detection.yaml:       detect_objects only: masks and a checkpoint, no features
object_analysis_scoped.yaml: the three steps per image, then
                             summarise_population (scope: compartment)
                             compare_populations  (scope: carrier)
population_plots.yaml:       plot_population: a PCA or UMAP of a detected population
```

This is the workflow that uses most of what the engine offers: a step in its
own environment (Cellpose), a model kept loaded between images, several
workers for the classical steps, and steps that wait for a compartment or a
carrier to be complete.

→ [`workflows/object_analysis/README.md`](../../workflows/object_analysis/README.md)

## driver_configuration

Measures two things a [ZMART driver](https://github.com/thomdehoog/ZMART-drivers)
has to know before it can steer the stage by what it sees in the picture.
Both are measured from pictures alone, so the same steps serve every
microscope.

```text
orientation.yaml:      measure_orientation     which way the picture is turned relative to the stage
objective_pair.yaml:   measure_objective_pair  how far apart two objectives look and focus
```

**Orientation**: a camera is often mounted a quarter- or half-turn away from
the stage's own X and Y, and some settings mirror the image. From three
pictures of the same field, one at home and one after a known move along
each axis, the step reports the turn and how much specimen one pixel covers.

**Objective pair**: a target found under a low-power overview lens has to be
imaged again under a high-power one, and the two lenses do not look at
exactly the same place or focus at the same height. The step measures both
offsets.

Both steps write a review picture so you can check the measurement by eye.

→ [`workflows/driver_configuration/README.md`](../../workflows/driver_configuration/README.md)

## Adding your own

A workflow is a folder with the same four parts: `pipelines/` with the
recipes, `steps/` with one file per step, `environments/` with the scripts
that build its conda environments, and `tests/`. How to write a step and
what goes where is in
[Implement an analysis step](../2_implement_an_analysis_step/README.md).
