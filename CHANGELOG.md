# Changelog

## Unreleased

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
