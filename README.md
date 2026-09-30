# ZMART Analysis

[![tests](https://github.com/thomdehoog/ZMART-analysis/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/thomdehoog/ZMART-analysis/actions/workflows/test.yml)
[![python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

ZMART Analysis runs image-analysis pipelines while a microscope is still
acquiring, fast enough for the results to decide what to image next. It is
the analysis half of ZMART, the microscopy toolkit of the Center for
Microscopy and Image Analysis (ZMB), University of Zurich; the other half,
[ZMART Microscopy](https://github.com/thomdehoog/ZMART-microscopy), drives
the microscope.

## The problems it solves

1. **Reproducible, shareable analysis.** A pipeline is a YAML recipe that
   names every step and every parameter. Sharing the recipe shares exactly
   what was done, and every result records the environment, the Python
   version and the package versions each step ran with.
2. **Tools that cannot share an environment.** Cellpose pins one torch,
   another model pins another, and neither agrees with the plotting
   library. Each step file names the conda environment it needs, so a tool
   anyone brings to the facility becomes a step without breaking the
   others. Steps that name none simply run in the environment you started
   from.
3. **Analysis fast enough for live acquisition.** Each environment gets a
   worker process that stays alive between jobs, with its model already
   loaded, so a tile is analysed the moment it lands instead of waiting for
   a fresh start. The recipe sets how many workers a step may use in
   parallel: one for a GPU model, many for CPU work.
4. **Analysis over a field, a well, a plate.** Data is submitted while it
   is acquired. A step can be scoped so that it runs once a whole well is
   done, on everything collected for it, and a later step once the whole
   plate is done. Per-object measurements, then population statistics per
   well, then one summary per plate.

## What it does not do

- It does not move the microscope or read the camera. ZMART Microscopy does
  that and calls this.
- It is not a cluster scheduler or a queue service. It runs on the
  acquisition PC, or one machine beside it.
- It does not store results. Steps write files next to the data; the
  engine keeps nothing once you have collected a result.
- It does not run untrusted code. Step files are Python written by the
  workflow author and run with that author's rights.

## The workflows that ship

| Workflow | What it answers | Environment |
|---|---|---|
| `focus/` | Where in a z-stack the sample is sharp. Four sharpness measures are scored on every stack; the recipe chooses which one decides. | `ZMART--focus--main` |
| `object_analysis/` | Which objects are in an image, and their size, shape, intensity and texture, as one table. Cellpose for the robust path, a watershed detector for the fast one. The plate recipe adds a population summary per well and a comparison of the wells per plate. | `ZMART--object_analysis--cellpose`, `--classical` |
| `population/` | A two-axis picture of a whole population, by principal components or UMAP, with the explained variance so the picture can be read. | `ZMART--population--main` |
| `driver_configuration/` | How the camera's pixels map onto the stage, and where two objectives look relative to each other, measured from images. | `ZMART--driver_configuration--main` |

Each workflow folder holds `pipelines/` (the recipes), `steps/` (one Python
file per step), `tests/`, and `environments/setup_env.py`, which creates the
conda environment the steps name. The image reader every step shares is
`workflows/_image_io.py`; the sharpness measures are in
`workflows/_focus_metrics.py`.

## Install

```bash
git clone https://github.com/thomdehoog/ZMART-analysis.git
cd ZMART-analysis
conda create -n zmart-analysis python=3.12 -y
conda activate zmart-analysis
python -m pip install -e ".[test]"
python workflows/focus/environments/setup_env.py        # one per workflow you use
pytest -m "not cellpose and not conda_env and not pooch"
```

Python 3.10 or newer, and conda, because that is how a step gets its own
environment.

## A first pipeline

A pipeline is a recipe, one or more steps, and a few lines that submit
work. This one doubles a number.

`pipeline.yaml`:

```yaml
metadata:
  functions_dir: "./steps"

hello:
  - double_it:
      max_workers: 4       # up to four of these at once
```

`steps/double_it.py`:

```python
def run(pipeline_data, state, **params):
    n = pipeline_data["input"]["n"]
    pipeline_data["doubled"] = n * 2
    return pipeline_data
```

`run.py`:

```python
import time
from engine import Engine

with Engine() as engine:
    engine.register("hello", "pipeline.yaml")
    engine.submit("hello", {"n": 21})
    while not (results := engine.results("hello")):
        time.sleep(0.05)

print(results[0]["doubled"])  # 42
```

`state` is a dictionary that survives from one call to the next inside a
worker. Put a loaded model in it on the first call and every later call
reuses it; that is what keeps the worker warm.

## A step in its own environment

The step file names the environment it needs, beside the imports that need
it. The recipe never does: it describes only the analysis.

```python
METADATA = {"environment": "ZMART--object_analysis--cellpose", "max_workers": 1}

def run(pipeline_data, state, **params):
    from cellpose import models
    if "model" not in state:
        state["model"] = models.CellposeModel(gpu=params.get("gpu", False))
    ...
```

The engine reads `METADATA` without importing the file, so the heavy
imports happen only inside the worker, in the right environment. A step
that should run somewhere else is a separate step file: the watershed
detector, `detect_objects_fast`, names the light classical environment
while `detect_objects` names the Cellpose one.

## Steps over a field, a well, a plate

Give a step a `scope`, and it waits until the caller says that unit is
complete, then runs once over everything collected for it. The plate
recipe of object analysis, `object_analysis_plate.yaml`, works this way:

```yaml
object_analysis:
  - detect_objects:        # every tile, as soon as it lands
  - extract_classical_features:
  - build_object_table:
  - summarise_well:        # once per well: the population, its profile, its PCA
      scope: well
  - summarise_plate:       # once per plate: the wells compared, odd ones flagged
      scope: plate
```

```python
engine.submit("plate", tile, scope={"plate": "P1", "well": "B3"})
...
engine.submit("plate", last_tile, scope={"plate": "P1", "well": "B3"}, complete="well")
...
engine.submit("plate", very_last_tile, scope={"plate": "P1", "well": "H12"},
              complete=["well", "plate"])
```

The engine never guesses when a well is done. The acquisition knows, and
says so. A well is matched together with its plate, because well names
repeat on every plate, so two plates can be acquired at once without their
wells mixing.

## Reading the results

`engine.results(name)` returns every finished job since you last asked,
each a dictionary with the step outputs under the step's name, and a
`provenance` entry per step naming its environment, Python version, a
fingerprint of every installed package, and the versions of the packages
the step imported. Failures
are listed by `engine.status(name)`, with the step and the error. Steps
that write files put them in an `analysis/` folder beside the `data/`
folder the image came from.

## Testing

```bash
pytest -m "not cellpose and not conda_env and not pooch"   # what CI runs
pytest workflows/focus                                      # one workflow
pytest                                                      # everything, given the environments
```

Tests marked `cellpose` need Cellpose and torch in the active environment,
`conda_env` need the workflow environments, and `pooch` download public
sample images. All three skip cleanly when what they need is missing.

## Citing

See [CITATION.cff](CITATION.cff). The sharpness measures follow Brenner
(1976), Vollath (1987) and the comparison in Pertuz, Puig and Garcia
(2013); object detection uses Cellpose (Stringer et al. 2021) and
scikit-image; population layouts use scikit-learn and umap-learn.

## License

MIT. See [LICENSE](LICENSE).
