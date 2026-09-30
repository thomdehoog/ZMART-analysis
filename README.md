# ZMART Analysis

[![tests](https://github.com/thomdehoog/ZMART-analysis/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/thomdehoog/ZMART-analysis/actions/workflows/test.yml)
[![python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

ZMART Analysis is the analysis engine of ZMART, ZMB's Microscopy-Agnostic
Research Toolkit, developed at the Center for Microscopy and Image Analysis
(ZMB), University of Zurich. It analyses images while the microscope is
still acquiring them, and its results decide what the experiment images
next. Its partner, [ZMART Microscopy](https://github.com/thomdehoog/ZMART-microscopy),
drives the microscope.

## The problems it solves

1. **Reproducibility.** An analysis is written down as a recipe that
   anyone can read, share and run again.
2. **Compatibility.** Tools that need conflicting software can still be
   used together in one analysis.
3. **Real-time analysis.** Results arrive while the microscope is still
   working, fast enough to act on.
4. **Scope.** Some questions are about one tile, others about a whole
   compartment or carrier; each is answered as soon as its data is complete.

## Install

```bash
git clone https://github.com/thomdehoog/ZMART-analysis.git
cd ZMART-analysis
conda create -n zmart-analysis python=3.12 -y
conda activate zmart-analysis
python -m pip install -e ".[test]"
python workflows/focus/environments/setup_env.py   # once for each workflow you use
```

Python 3.10 or newer and conda are needed. Conda is how each step gets its
own software environment.

## 1. Reproducibility

An analysis is a YAML recipe: the steps, in order, with every parameter
written out. The recipe is the record of what was done. Share the file, and
a colleague runs exactly the same analysis.

```yaml
focus:
  - score_focus:
      metric: brenner      # brenner | dct | vollath_f4 | intensity
      channel: 0
      skip_ends: 2
```

Every result also records, for each step, the software environment it ran
in, the Python version, and the versions of the packages it used. The
recipe says what was asked for, and the result says what actually ran.

## 2. Compatibility

Image-analysis tools often cannot be installed side by side. Cellpose needs
one version of torch, another model needs another, and neither agrees with
the plotting library. Here each step runs in its own conda environment, named
at the top of the step file:

```python
METADATA = {"environment": "ZMART--object_analysis--cellpose"}
```

Steps that name no environment run in the one you started from. So you can
keep everything in one environment, and split off only the step that
conflicts. A tool someone brings to the facility becomes one more step,
without breaking the others.

## 3. Real-time analysis

Starting a program and loading a deep-learning model onto the GPU can take
many seconds. That is too slow to repeat for every tile. Instead, each
environment gets a worker that starts once and stays running. The engine
sends it tile after tile, and the model it loaded for the first tile is
still there for the next:

```python
def run(pipeline_data, state, **params):
    if "model" not in state:          # only on the first tile
        state["model"] = load_the_model()
    ...
```

The recipe also says how many copies of a step may run at the same time:
one for a model on the GPU, many for work on the processor.

```yaml
  - detect_objects_fast:
      max_workers: 12
```

## 4. Scope

A sample is divided into four levels, narrowest first: a **tile**, a
**group** of tiles, a **compartment** and a **carrier**. Each is a plain
number, so a compartment can be a well of a plate, a region of a slide or
part of a cleared sample. A step with a `scope` waits until the acquisition
says that unit is complete, then runs once on everything collected for it:

```yaml
object_analysis:
  - detect_objects:            # every tile, as soon as it lands
  - extract_classical_features:
  - build_object_table:
  - summarise_population:      # each compartment, once it is complete
      scope: compartment
  - compare_populations:       # each carrier, once it is complete
      scope: carrier
```

```python
engine.submit("scoped", tile, scope={"carrier": 1, "compartment": 3})
engine.submit("scoped", last_tile, scope={"carrier": 1, "compartment": 3},
              complete="compartment")
```

The engine never guesses when a unit is done. The acquisition knows, and
says so.

## Documentation

- [How the engine works](docs/how-the-engine-works.md): recipes, workers,
  scopes, and what the engine reports.
- [Writing a step](docs/writing-a-step.md): the one function a step needs,
  and how to give it its own environment.
- [Folder structure](docs/folder-structure.md): where recipes, steps, tests
  and environments live.
- The workflows that ship, each with its own notes:
  [focus](workflows/focus/pipelines/focus.yaml),
  [object analysis](workflows/object_analysis/README.md), with its
  population summaries and plots, and
  [driver configuration](workflows/driver_configuration/pipelines/orientation.yaml).

## Citing and license

See [CITATION.cff](CITATION.cff) for how to cite ZMART Analysis. It is
released under the MIT license; see [LICENSE](LICENSE).
