# `object_analysis` workflow

Object-centered analysis for one acquired image tile.

```text
object_analysis.yaml:       detect_objects -> extract_classical_features -> build_object_table
object_analysis_fast.yaml:  the same, detection placed in the classical environment (method: fast),
                            features without the per-object texture crops (glrlm, lbp, fft)
object_detection.yaml:      detect_objects   (persist_only: masks and checkpoint, no features)
object_analysis_scoped.yaml: the three steps per tile, then
                             summarise_population (scope: compartment) its objects as a population
                             compare_populations  (scope: carrier)     the compartments side by side
population_plots.yaml:      plot_population  (on the operator's ask) a PCA or UMAP of a detected population
```

The scoped recipe is for runs that tell the engine when a compartment and a
carrier are complete. Each compartment's population is described once all
its tiles are measured: per-feature medians and quartiles, its profile (the
median of each feature), and principal components with their explained
variance. Each carrier then lays its compartment profiles side by side and
gives every compartment a robust z-score per feature, flagging those more
than `outlier_z` spreads from the median compartment. These are the default
aggregation and normalisation of image-based profiling (pycytominer's
`aggregate` and `mad_robustize`), computed with pandas, numpy and
scikit-learn in the classical environment. The two steps read their level
from the recipe, so `scope: group` and `scope: compartment` summarise per
tile set instead; the recipe's `levels` keeps a group inside its
compartment either way. A carrier comparison counts the compartments that
failed (`n_failed_units`) and the tiles of compartments not closed when the
carrier was (`n_not_closed`), and every summary and comparison carries its
`lineage`: the tiles under it and where every step below it ran.

The population plot is the one recipe here that does not run per tile. Once
a whole overview has been detected and its object table written, the
operator can ask for a plot of that population: the first two principal
components, or a UMAP laid out from the first fifty. Objects that are alike
stand together in the plot, whatever mix of features makes them alike, so
the population's own structure can be gated on as well as single features.
It uses the same conditioning and PCA as the compartment summary
(`workflows/shared/population.py`), so a principal component means the same thing
in both. A UMAP over half a million objects takes minutes, which is why it
runs on request and never during detection.

Each step file names its own environment: `detect_objects` the Cellpose one,
`detect_objects_fast` the classical one, so the fast pipeline never spawns
the torch worker. A recipe does not set environments. Both pipelines answer
under `pipeline_data["object_analysis"]`.

## Environments

One environment per model, and one for everything classical:

```text
ZMART--object_analysis--classical   scipy, scikit-image, the readers: the fast detector and the features
ZMART--object_analysis--cellpose    torch and Cellpose, the readers: the robust detector
ZMART--object_analysis--umap        pandas, scikit-learn, umap-learn: the population plot
```

Create each with `python environments/setup_env.py --step <name>`. The
`umap` one has no readers and no torch: `plot_population` reads the object
table the other steps wrote, not the images. It is kept apart from the
classical environment because umap-learn brings the numba compiler with it,
which the classical steps have no use for.

The rule: a model environment holds one model and the readers (tifffile,
ngio, ome-types), never the features. Cellpose pins one torch, StarDist pins
TensorFlow, the next model will pin another CUDA, and those pins fight each
other and drag their own numpy along. Kept apart, a model upgrade breaks only
its own worker, and a model that will not install on a rig disables one
option rather than all detection. A new model is a branch in
`detect_objects.py` that imports its model lazily, a profile in
`environments/setup_env.py`, and a pipeline YAML placing the step in that
environment. The fast detector and the features stay together: both are scipy
and scikit-image on numpy, released together, and splitting them would cost a
second worker spawn and a pickle hop of the image per field for no isolation.

Every parameter lives in the pipeline YAML with its default, and each can be
overridden per submission — which is how the operator page tunes detection on
one position without registering a pipeline of its own. The step docstrings
are the reference for what each takes and returns.

## Input

Submit one tile at a time:

```python
{
    "image_path": "path/to/position",      # OME-Zarr position or OME-TIFF
    "tile_id": ["R0", 3, 7],
    "tile_stage_xy_um": [10000.0, 15000.0],
    "tile_z_um": 2500.0,               # capture height; optional
    "source_pixel_size_um": [0.65, 0.65],
    "source_image_size_px": [2048, 2048],  # (nx, ny)
    "image_to_stage": [[0.0, -1.0], [1.0, 0.0]],
    "channels": None,                      # up to three; [0, 2] to choose
    "gpu": False,
}
```

## Tuning notes

- Prefer `min/max_equivalent_diameter_um` over `min/max_area_px` for size
  bounds that survive a pixel-size change; one kind per side, not both.
- `border_margin_px` rejects objects within a band of each tile edge. Tiles
  overlap, so an edge object is very likely to appear again in the neighbour;
  about half the overlap keeps each object once.
- `segmentation_binning` runs Cellpose at 1/n linear resolution; masks are
  rescaled to full size before features. Choose it from segmentation quality,
  not only speed.
- `cellprob_threshold` lower = larger/more masks; `flow_threshold` is
  Cellpose's flow-consistency QC; `niter` helps very long objects.

## Output

The result lands under `pipeline_data["object_analysis"]` as the object
table: per-object features plus `stage_x_um`/`stage_y_um` (placed via the
tile's own geometry), `tile_name`, and `object_id`.

`detect_objects` also writes `masks.tif`, `raw_masks.tif` and
`detection_checkpoint.json` under `<analysis>/tiles/<short_name>/` — into the
`analysis` folder beside the `data` the image came from, or `output_dir` when
given, or nowhere when the image is outside an acquisition and no
`output_dir` names a place. The checkpoint records the effective parameters,
a hash of the true mask-generation parameters, and content hashes of the
image and masks, so a run is reproducible from what actually ran.

## The Cellpose model

Cellpose downloads its model (about 1.2 GB) the first time it runs, into
`.cellpose` in your home folder. To keep it elsewhere, for example where a
Windows profile has a size limit, set `CELLPOSE_LOCAL_MODELS_PATH` to that
folder before the first run.
