# Changelog

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
