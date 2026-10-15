# 3. The workflows we use

Three workflows ship with ZMART Analysis. Each is a folder under
[`workflows/`](../../workflows) with its pipelines, its steps, its environment
scripts and a README that explains its settings.

| Workflow | What it does | What it needs |
|---|---|---|
| [`focus`](#focus) | Finds the sharp height in a z-stack. | One small environment. No GPU. |
| [`object_analysis`](#object_analysis) | Detects objects, measures them and describes populations. | Up to three environments. A GPU for Cellpose. |
| [`driver_configuration`](#driver_configuration) | Measures what a driver needs to know about a microscope. | One environment. No GPU. |

How to run a workflow is in [part 1](../1_use_the_engine/README.md). How to
write one is in [part 2](../2_implement_an_analysis_step/README.md).

## focus

Every plane of a z-stack gets a sharpness score. The height where the score
peaks is the focus. This is how the microscope finds the tissue before it
images a position.

```text
focus.yaml:   score_focus   scores every plane and reports the height of the peak
```

It is one step in one environment. Try this workflow first.

→ [`workflows/focus/README.md`](../../workflows/focus/README.md) explains
how to choose a sharpness measure.

## object_analysis

Finds the objects in an image, measures each one, and returns a table with
one row per object. Detection uses Cellpose on a GPU, or a faster classical
method that needs no GPU. Two further steps describe the objects of a
compartment as a population and compare the compartments of a carrier.

```text
object_analysis.yaml:        detect_objects -> extract_classical_features -> build_object_table
object_analysis_fast.yaml:   the same, with classical detection and fewer texture features
object_detection.yaml:       detect_objects only: masks and a checkpoint
object_analysis_scoped.yaml: the three steps per image, then
                             summarise_population (scope: compartment)
                             compare_populations  (scope: carrier)
population_plots.yaml:       plot_population: a PCA or UMAP of a population
```

Read this workflow to see everything the engine offers. It has steps in
separate environments, a model that stays loaded, and steps that wait for a
compartment or a carrier.

→ [`workflows/object_analysis/README.md`](../../workflows/object_analysis/README.md)
describes the input, the output and the environments.

## driver_configuration

Measures two things that a [ZMART driver](https://github.com/thomdehoog/ZMART-drivers)
must know before it can steer the stage by what it sees. Both are measured
from pictures alone, so the same steps work on every microscope.

```text
orientation.yaml:      measure_orientation     how the camera is turned relative to the stage,
                                               and how much specimen one pixel covers
objective_pair.yaml:   measure_objective_pair  how far apart two objectives look and focus
```

Both steps write a review picture, so you can check the measurement by eye.

→ [`workflows/driver_configuration/README.md`](../../workflows/driver_configuration/README.md)
explains which pictures each measurement needs.
