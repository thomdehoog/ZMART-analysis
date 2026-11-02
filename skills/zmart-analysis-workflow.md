---
name: zmart-analysis-workflow
description: Build or port an image analysis into a ZMART Analysis workflow - split it into steps, put the dependencies in separate conda environments, write the pipeline YAML and the tests, and keep it maintained. Use when someone wants to run their analysis on the ZMART Analysis engine.
---

# Build a ZMART Analysis workflow

You help a user turn an image analysis into a workflow for the ZMART Analysis
engine (`pip install "zmart-analysis @ git+https://github.com/ZMB-UZH/ZMART-analysis"`).
Before you start, read in that repository:

- `docs/1_use_the_engine/README.md`: how a pipeline runs (register, submit,
  scopes, levels, the job board).
- `docs/2_implement_an_analysis_step/README.md`: the rules for steps,
  environments and pipelines.
- `zmart_analysis/README.md`: how the engine works inside. Open the source
  only when the docs do not answer a question.

Start from a copy of the closer example:

- `examples/focus`: one environment.
- `examples/object_analysis`: segmentation and measurement, several
  environments.

## 1. Ask first

Ask, one question at a time, and wait for the answers:

1. What should the analysis return, and what does the microscope do with it?
2. Is there existing code (a script, a notebook, a Fiji macro, a repository)?
   Where is it?
3. What goes in: file format, dimensions, channels, pixel size.
4. Does it run per image, or once per group (a well, a plate, the
   experiment)? Over time?
5. Which computer runs it, and does it have a GPU?
6. Where should the workflow folder live?

## 2. Split it into steps

- The first step is `read_images`: it opens the files, so no later step does.
- One step per clear job: segment, measure, summarise. A step that waits for a
  group gets a `scope` in the pipeline.
- When porting, keep the user's method. Do not change the algorithm or its
  defaults without asking.

## 3. Split the dependencies into environments

- Group steps whose packages install together. Put heavy or conflicting
  packages (PyTorch, Cellpose, TensorFlow) in their own environment, so the
  other steps never load them.
- Name each environment `ZMART--<workflow>--<name>`; one environment is
  `main`.
- For each, write `environments/<name>.yml` (conda-forge, `nodefaults`, Python,
  pip with `-r <name>.txt`) and build it with `conda env create -f`.
- When the tests pass, write the lock file with `pip freeze > <name>.txt`.

## 4. Write the steps

Each step is one file in `steps/`, in this order: a docstring (what it does,
takes and hands on), imports, `METADATA`, `run`, then helpers whose names
start with `_`.

- `METADATA` is plain values, never computed, and names the `environment`.
- `run(pipeline_data, state, **params)` adds its output under the step's name
  and returns `pipeline_data`. A scoped step returns a new dictionary.
- Return plain Python and NumPy arrays.
- Keep a loaded model in `state`.

## 5. Write the pipeline

`pipelines/<name>.yaml`: `functions_dir: "../steps"`, `levels` if any step has
a `scope`, and every parameter with a value and a comment.

## 6. Test

- Call each `run` directly in `tests/`; pass a scoped step its parts by hand.
- Run the pipeline through the engine on real images. Make a new `Engine`
  after every edit; a running worker keeps the old code.
- When porting, run the original code and the workflow on the same images
  and compare the results. Show the user any difference.

## 7. Hand over

Write the workflow's `README.md`: what it does, how to make the environments,
how to choose the settings. Tell the user how to maintain it: change a lock
file on purpose, rerun the tests after any change, raise a step's `version`
in `METADATA` when its output changes.
