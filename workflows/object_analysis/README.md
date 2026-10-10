# `object_analysis` workflow

Finds the objects in an image, measures each one, and returns a table with
one row per object. It works on one *tile* at a time. A tile is one image of
a larger field, submitted as soon as it is acquired.

## The recipes

| Recipe | What it runs |
|---|---|
| `object_analysis.yaml` | `detect_objects` → `extract_classical_features` → `build_object_table`. Detection uses Cellpose. |
| `object_analysis_fast.yaml` | The same, with `detect_objects_fast`: a watershed that needs no GPU. It leaves out the texture features that need a crop of every object (`glrlm`, `lbp`, `fft`). |
| `object_detection.yaml` | `detect_objects` only. It writes the masks and a checkpoint, and measures no features. |
| `object_analysis_scoped.yaml` | The three steps per tile. Then `summarise_population` describes each compartment, and `compare_populations` compares the compartments of each carrier. |
| `population_plots.yaml` | `plot_population`: a PCA or UMAP of a detected population, made on request. |

Every recipe puts its result under `pipeline_data["object_analysis"]`.

Every parameter is in the recipe, with its default. The docstring of each
step, in `steps/`, says exactly what the step takes and returns.

## Input

Submit one tile at a time:

```python
{
    "image_path": "path/to/position",      # OME-Zarr position or OME-TIFF
    "tile_id": ["R0", 3, 7],
    "tile_stage_xy_um": [10000.0, 15000.0],
    "tile_z_um": 2500.0,                   # capture height; optional
    "source_pixel_size_um": [0.65, 0.65],
    "source_image_size_px": [2048, 2048],  # (nx, ny)
    "image_to_stage": [[0.0, -1.0], [1.0, 0.0]],
    "channels": None,                      # up to three; [0, 2] to choose
    "gpu": False,
}
```

These keys are optional. The operator page in ZMART Microscopy sends them
when it has them:

| Key | What it is |
|---|---|
| `method` | `"robust"` (Cellpose, the default) or `"fast"` (the watershed). The detector in the recipe decides which of the two can run. |
| `z_selection` | Which plane of a z-stack to detect on: an index, `"mid"` (the default), or `"max"` / `"mean"` for a projection. |
| `extra_channel_paths` | The other channels, for a frame that is stored as one file per plane. They are added to the detection image, so the features are measured in every colour. |
| `extra_channel_indices` | The same for a frame that is stored as one position: the indices of the channels to read from it. |
| `synthetic_pixels` | For images from a mock microscope: how the pixels were made. It is copied into the checkpoint for the record. |
| `output_dir` | Where to write the masks and the checkpoint. The default is the `analysis` folder next to the `data` folder the image came from. |

You can also give any detection setting of the recipe here, such as
`diameter`, `cellprob_threshold` or `border_margin_px`. It then replaces
the recipe's value for this one image. This is how the operator page tunes
detection on one position.

### Try it on one image

To try the Cellpose recipe from a shell, without writing the input
yourself:

```bash
python run_pipeline.py image.tif --pixel-size-um 0.65,0.65
```

This runs `object_analysis.yaml` through the engine and prints the number
of objects. `python run_pipeline.py --help` lists the flags. There is one
for each input key.

## Output

The result is the object table, under `pipeline_data["object_analysis"]`.
Each row holds the features of one object, its `object_id`, its `tile_name`,
and its position on the stage as `stage_x_um` and `stage_y_um`. The position
is worked out from the geometry of the tile.

`detect_objects` also writes three files to `<analysis>/tiles/<short_name>/`:
`masks.tif`, `raw_masks.tif` and `detection_checkpoint.json`. Where
`<analysis>` is depends on the input:

- With `output_dir` given, the files go there.
- Otherwise they go to the `analysis` folder next to the `data` folder the
  image came from.
- If the image is not part of an acquisition and no `output_dir` is given,
  nothing is written.

The checkpoint records the parameters that were actually used, a hash of
the parameters that shaped the masks, and hashes of the image and the
masks. With it, a run can be reproduced from what really ran.

## The scoped recipe

`object_analysis_scoped.yaml` is for runs that tell the engine when a
compartment and a carrier are complete. It adds two steps.

**`summarise_population`** runs once per compartment, when all its tiles
have been measured. It describes the objects of the compartment as a
population:

- the median and quartiles of every feature,
- the compartment's *profile*, which is the median of each feature,
- principal components, with the variance each one explains.

**`compare_populations`** runs once per carrier. It puts the profiles of
the compartments side by side and gives every compartment a robust z-score
per feature. A compartment that lies more than `outlier_z` spreads from the
median compartment is flagged. The result also counts the compartments that
failed (`n_failed_units`) and the tiles whose compartment was not complete
when the carrier was (`n_not_closed`).

These are the standard aggregation and normalisation of image-based
profiling, the same as `aggregate` and `mad_robustize` in pycytominer. Here
they are computed with pandas, numpy and scikit-learn.

Both steps take their level from the recipe. To summarise per group
instead, give them `scope: group` and `scope: compartment`. The `levels`
entry of the recipe keeps each group inside its own compartment.

Every summary and comparison carries its `lineage`: the tiles it was built
from and where every earlier step ran.

## The population plot

`population_plots.yaml` is the one recipe here that does not run per tile.
When a whole overview has been detected and its object table is written,
the operator can ask for a plot of that population. The plot shows the
first two principal components, or a UMAP made from the first fifty.

Objects that are alike end up close together in the plot, whichever mix of
features makes them alike. So you can select objects by the structure of
the population, not only by single features.

The plot prepares the data and computes the PCA in the same way as the
compartment summary (`workflows/shared/population.py`). A principal
component therefore means the same thing in both.

A UMAP of half a million objects takes minutes. That is why the plot is
made on request and never during detection.

## Environments

There is one environment per model, and one for everything classical:

| Environment | Holds | Used by |
|---|---|---|
| `ZMART--object_analysis--classical` | scipy, scikit-image and the image readers | The fast detector, the features and the two population steps |
| `ZMART--object_analysis--cellpose` | PyTorch, Cellpose and the image readers | `detect_objects` |
| `ZMART--object_analysis--umap` | pandas, scikit-learn and umap-learn | `plot_population` |

Create each one with `python environments/setup_env.py --step <name>`.

Each step file names its own environment, so a recipe only starts the
workers it needs. The fast recipe never loads PyTorch.

### Why they are split this way

A model environment holds one model and the image readers (tifffile, ngio,
ome-types), and never the features. Each model insists on its own versions:
Cellpose needs one PyTorch, StarDist needs TensorFlow, and the next model
will need yet another CUDA. These requirements conflict, and each brings its
own numpy. Kept apart, an upgrade of one model can only break its own
worker. A model that will not install on one microscope computer removes
one option, not all detection.

The fast detector and the features share an environment on purpose. Both
are built on scipy and scikit-image. Splitting them would start a second
worker and copy every image between the two, and it would separate nothing
that conflicts.

The `umap` environment has no image readers and no PyTorch, because
`plot_population` reads the object table and not the images. It is kept
apart from the classical environment because umap-learn brings the numba
compiler with it, which the classical steps do not need.

### Add a model

1. Write a module next to `parts/cellpose_model.py`. Import the model
   inside the function that uses it, so the module loads without it.
2. Add a branch for it in `segment_position` in `detect_objects.py`.
3. Add its package list to `environments/setup_env.py`.
4. Write a recipe that uses a step in the new environment.

## Tuning notes

- **Size limits.** Prefer `min_equivalent_diameter_um` and
  `max_equivalent_diameter_um` over `min_area_px` and `max_area_px`. A limit
  in micrometres stays right when the pixel size changes. Use one kind per
  side, not both.
- **`border_margin_px`** rejects objects within this distance of a tile's
  edge. Tiles overlap, so an object at the edge very likely appears again
  in the neighbouring tile. A margin of about half the overlap keeps each
  object once.
- **`segmentation_binning`** runs Cellpose on an image that is `n` times
  smaller in each direction. The masks are scaled back to full size before
  the features are measured. Choose it by the quality of the segmentation,
  not only by speed.
- **`cellprob_threshold`**: a lower value gives larger masks and more of
  them.
- **`flow_threshold`** is Cellpose's own quality check on its masks.
- **`niter`**: a higher value helps with very long objects.

## The Cellpose model

Cellpose downloads its model (about 1.2 GB) the first time it runs, into
`.cellpose` in your home folder. To keep it somewhere else, for example
when a Windows profile has a size limit, set `CELLPOSE_LOCAL_MODELS_PATH`
to that folder before the first run.
