# Changelog

## 5.0.0rc2 (2026-10-16) -- two workflows

The workflows are two self-contained folders, `alignment` and
`object_analysis`; `shared/` and `object_analysis/parts/` are gone. What the
steps compute is unchanged: the same masks, tables, focus heights,
orientations and registrations on the same input.

- **`read_images` is the first step of every image pipeline.** It opens the
  files and hands on the pixels; no step after it opens a file. Each workflow
  has its own.
- **`alignment`** holds `focus.yaml`, `orientation.yaml` and
  `registration.yaml` (was `objective_pair.yaml`; `measure_objective_pair` is
  now `measure_registration`), in one environment, `ZMART--alignment--main`.
  It replaces `ZMART--focus--main` and `ZMART--driver_configuration--main`:
  remove those with `conda env remove -n <name>` on a microscope PC.
- **`object_analysis`**: `object_analysis.yaml` is
  `object_analysis_cellpose.yaml`, `object_analysis_fast.yaml` is
  `object_analysis_watershed.yaml`, and `population_plots.yaml` is
  `reduce_dimensions.yaml`. `detect_objects` is `segment_cellpose`,
  `detect_objects_fast` is `segment_watershed`, `extract_classical_features`
  is `measure_objects`, and `plot_population` is `reduce_dimensions`; each
  publishes under its new name. The run records move with the pipeline
  names, to `analysis/<new name>/`.
- **Removed, because nothing called them:** `summarise_population`,
  `compare_populations`, `object_analysis_scoped.yaml`,
  `object_detection.yaml` (with `persist_only`), `run_pipeline.py`, the
  review pictures of the orientation and objective-pair steps, and the
  reader's PNG/JPEG, scikit-image-sample and `to_physical` paths. The
  classical environment no longer installs pandas and scikit-learn, and the
  Cellpose environment no longer installs the image readers (`read_images`
  runs in the classical one).
- **Cellpose is pinned below 4.2.** Cellpose 4.2 made a new network
  (`cpsam_v2`) the default, which changes the masks and downloads 1.2 GB on
  first use. Moving to it is a decision of its own.
- **Environment set-up** names the environment's own interpreter by its
  path. `conda run -n <env> pip` started base's pip when base stood first on
  PATH, and the install failed. Each environment's check now runs its own
  steps on a small image; the Cellpose check says which device it used.
- Tests: each workflow runs its pipelines on real microscopy from
  scikit-image's sample data (`*real_samples.py`), and
  `tests/test_workflow_layout.py` checks that a workflow imports nothing from
  another and that only `read_images` opens an image.

## 5.0.0rc1 (2026-10-16) -- engine v5

To update a call: `engine.register(name, yaml)` becomes
`engine.register(name, yaml, output)`, and every step's `METADATA` names its
`environment`.

- **Every step names its environment.** `register` refuses a step whose
  `METADATA` has no `environment`. `build_object_table` names the classical
  one.
- **`register(name, yaml, output)`.** A pipeline writes into
  `<output>/analysis/<name>/`: a copy of its YAML (`pipeline.yaml`), the run
  record (`run.jsonl`: one line per register, submit, job end and
  deregister, each on disk before the next), and the files its steps keep
  (`metadata["output"]`).
- **Data between steps goes through files.** A step's output is written to
  a folder, arrays as `.npy`, and the next step reads it back. The engine
  needs no NumPy, and steps in different NumPy versions can hand arrays to
  each other. What waits for a scope is a folder, and a scoped step reads
  its results one at a time, so the engine's memory stays flat however
  large the scope.
- A done job's `output` on the board is read from its folder when it is
  looked into; `deregister` returns the board read in full and then removes
  the hand-over files.
- `summarise_population` and `compare_populations` write their tables into
  `metadata["output"]`; their `output_dir` parameter is gone.
- **The job board (breaking).** `submit(pipeline, data, ...)` returns the
  job's id; ids count per pipeline from 1. `status(name)` is the board: every
  job with its `step`, `scope`, `state` (`queued`, `running`, `waiting`,
  `done`, `failed`, `cancelled`) and `output`, and `error` on a failed one.
  `status()` gives one board per pipeline. Completing a unit adds a scope job
  of its own. `results()` is gone, and so are the status counts, the
  `failures` list, `held`, and the `_phase`, `_scope` and `_scope_level`
  keys on every output. `submit`'s first argument is now `pipeline`.
- **`deregister(name, wait=True)`.** Ends a pipeline: its jobs finish (or,
  with `wait=False`, its running steps are killed and the rest cancelled),
  the workers no other registered pipeline needs are stopped, and the final
  board is returned. `shutdown()` deregisters every pipeline.
- **No idle timeout.** `Engine(idle_timeout=...)` is gone, and so is the
  reaper. A worker lives while a pipeline that needs it is registered.
- The submission index in `metadata` and `lineage` is the image job's id.
- What the docs and code called a recipe is called a pipeline everywhere.
- **Completing a level completes everything inside it.** `complete="carrier"`
  first closes that carrier's groups and compartments that are still open,
  narrowest first, then the carrier; `complete="experiment"` closes
  everything. An image that arrives after its unit closed opens it again.
  Which level is narrower comes from the pipeline's `levels`, or else from the
  order of its scoped steps. The scoped object analysis pipeline lists
  `experiment` as its widest level.
- The package is `zmart_analysis`: `from zmart_analysis import Engine`.
  The old name, `engine`, still imports.
- A step runs in the worker as a real module, registered under its file
  name, so a dataclass with postponed annotations, a class sent back in a
  result, or a sibling step importing it all work.
- Messages between the engine and its workers use pickle protocol 5.
- `conda_utils.py` is two modules: `conda.py` finds conda and its
  environments; `environments.py` is what `setup_env.py` and
  `clean_env.py` call.
- `object_analysis`: the per-tile steps are short and the work lives in
  `parts/`. The features step reads the image and masks from
  `detect_objects` and publishes under `extract_classical_features` with
  `n_objects` (it used to publish `extract_features` with `n_cells`, and
  `detect_objects` used to add `preprocess` and `segment`). The table under
  `object_analysis` is unchanged. The `channel_axis` setting is gone: the
  reader takes the axes from the image. The input keys the operator page
  sends are documented.
- The tests are plain pytest, and need pytest 9 or newer.

## 1.0.0rc1 (2026-10-07)

First release candidate. The step format, the recipe layout and the
`Engine` calls (`register`, `submit`, `status`, `results`, `shutdown`) are
settled in spirit; small changes may still happen before 1.0.

- Every step runs in the conda environment it names, in a worker that
  stays warm between images.
- Steps can be scoped to run once per group, compartment or carrier when
  the acquisition closes it, and every scoped result carries its lineage.
- Every result records the environment, Python version and package
  versions each step ran with.
- Workflows for focus scoring, object detection and measurement,
  population summaries and plots, and driver configuration.
- Tests run on Linux, macOS and Windows with Python 3.11 to 3.13.
