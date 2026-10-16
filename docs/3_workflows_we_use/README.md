# 3. The workflows we use

Two workflows ship with ZMART Analysis. Each is a folder under
[`workflows/`](../../workflows) with its pipelines, its steps, its environment
scripts, its tests and a README that explains its settings. Each needs nothing
outside its folder.

| Workflow | What it does | What it needs |
|---|---|---|
| [`alignment`](#alignment) | Finds the sharp height in a z-stack, how the camera is turned, and how two objectives line up. | One environment. No GPU. |
| [`object_analysis`](#object_analysis) | Detects objects, measures them, and lays out a population. | Up to three environments. A GPU for Cellpose. |

Every pipeline that works on images starts with `read_images`: it opens the
files and hands on the pixels, so no step after it opens a file.

How to run a workflow is in [part 1](../1_use_the_engine/README.md). How to
write one is in [part 2](../2_implement_an_analysis_step/README.md).

## alignment

What a microscope must know before it can steer by what it sees, measured
from pictures alone, so the same steps work on every microscope.

```text
focus.yaml:         read_images -> score_focus           the sharp height of a z-stack
orientation.yaml:   read_images -> measure_orientation   how the camera is turned, and the pixel size
registration.yaml:  read_images -> measure_registration  how far apart two objectives look and focus
```

The operator page runs `focus` for every point of a focus map. A driver's
set-up takes the pictures for `orientation` and `registration`.

→ [`workflows/alignment/README.md`](../../workflows/alignment/README.md)
explains how to choose a sharpness measure and which pictures each
measurement needs.

## object_analysis

Finds the objects in an image, measures each one, and returns a table with
one row per object. Cellpose finds them on a GPU; a watershed finds nuclei
faster, without one.

```text
object_analysis_cellpose.yaml:   read_images -> segment_cellpose -> measure_objects -> build_object_table
object_analysis_watershed.yaml:  the same, with segment_watershed and fewer texture features
reduce_dimensions.yaml:          reduce_dimensions: a PCA or UMAP of a population
```

Read this workflow to see steps in separate environments and a model that
stays loaded.

→ [`workflows/object_analysis/README.md`](../../workflows/object_analysis/README.md)
describes the input, the output and the environments.

