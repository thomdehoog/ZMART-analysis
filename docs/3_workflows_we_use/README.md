# 3. The workflows we use

Three workflows ship with ZMART Analysis. Each is a folder under
[`workflows/`](../../workflows) with its recipes, steps, environment
scripts and a README of its own.

```text
workflows/
  focus/                  find the sharp height in a z-stack
  object_analysis/        detect objects, measure them, describe populations
  driver_configuration/   measure what a driver needs to know about a microscope
```

To run one, see [Use the engine](../1_use_the_engine/README.md). To write
one, see [Implement an analysis step](../2_implement_an_analysis_step/README.md).

## focus

Finds the sharp height in a z-stack. Every plane gets a sharpness score,
and the height where it peaks is the focus. This is how the microscope
finds the tissue before it images a position.

```text
focus.yaml:   score_focus   every plane scored, the peak refined between planes
```

Four measures are scored on every stack; `metric` in the recipe says which
decides. `brenner` (default) gives a sharp peak on structured tissue,
`vollath_f4` holds up on dim noisy fluorescence, `dct` ignores brightness,
`intensity` is for sparse samples on a confocal.

One step, one environment, no GPU. Try this one first.

→ [`workflows/focus/README.md`](../../workflows/focus/README.md)

## object_analysis

Finds the objects in an image, measures each, and publishes a table with
one row per object. Detection is Cellpose on the GPU, or a faster classical
method without one. Over the scopes the acquisition completes, a
compartment's objects are described as a population and a carrier's
compartments are laid side by side.

```text
object_analysis.yaml:        detect_objects -> extract_classical_features -> build_object_table
object_analysis_fast.yaml:   the same, classical detection, fewer texture features
object_detection.yaml:       detect_objects only: masks and a checkpoint
object_analysis_scoped.yaml: the three steps per image, then
                             summarise_population (scope: compartment)
                             compare_populations  (scope: carrier)
population_plots.yaml:       plot_population: a PCA or UMAP of a population
```

This workflow uses most of what the engine offers: a step in its own
environment, a model kept loaded, several workers for the classical steps,
and steps that wait for a compartment or a carrier.

→ [`workflows/object_analysis/README.md`](../../workflows/object_analysis/README.md)

## driver_configuration

Measures two things a [ZMART driver](https://github.com/thomdehoog/ZMART-drivers)
must know before it can steer the stage by what it sees. Both are measured
from pictures alone, so the same steps serve every microscope.

```text
orientation.yaml:      measure_orientation     which way the picture is turned relative to the stage
objective_pair.yaml:   measure_objective_pair  how far apart two objectives look and focus
```

**Orientation**: from three pictures of one field, at home and after a
known move along each axis, the step reports how the camera is turned
relative to the stage and how much specimen one pixel covers.

**Objective pair**: a target found under a low-power lens is imaged again
under a high-power one, and the two do not look at the same place or focus
at the same height. The step measures both offsets.

Both write a review picture so you can check the measurement by eye.

→ [`workflows/driver_configuration/README.md`](../../workflows/driver_configuration/README.md)
