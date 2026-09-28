# ZMART Analysis

[![tests](https://github.com/thomdehoog/ZMART-analysis/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/thomdehoog/ZMART-analysis/actions/workflows/test.yml)
[![python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

ZMART Analysis runs image analysis while a microscope is still acquiring,
and hands the numbers back fast enough to decide what to image next. It is
the analysis half of ZMART, the microscopy toolkit of the Center for
Microscopy and Image Analysis (ZMB), University of Zurich. The other half,
[ZMART Microscopy](https://github.com/thomdehoog/ZMART-microscopy), moves
the stage and captures the images.

## Why it exists

Adaptive microscopy needs analysis in the loop: detect the cells in an
overview, pick the interesting ones, and go back to image them at high
resolution, all in one session. Five things kept getting in the way at the
facility, and each one became a design rule here.

1. **A run must be repeatable.** Every pipeline is a YAML recipe that names
   its steps and every parameter. Register the recipe, submit data, and the
   same recipe gives the same analysis next month.
2. **Tools do not share an environment.** Cellpose pins one version of
   torch, another model pins another, and neither agrees with the plotting
   library. Each step can name its own conda environment, so any tool a
   user brings to the facility becomes a step without breaking the others.
3. **Analysis must keep up with acquisition.** Worker processes stay warm
   between jobs, with the model already loaded, so a tile is scored the
   moment it lands rather than after a fresh start each time.
4. **CPU work should run in parallel.** The recipe says how many workers a
   step may use. GPU steps stay at one; feature extraction fans out.
5. **Some steps belong to a larger unit than one image.** A step can be
   scoped to run once per field, once per well, or once per plate, after
   every image in that unit has been analysed.

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
| `object_analysis/` | Which objects are in an image, and their size, shape, intensity and texture, as one table. Cellpose for the robust path, a watershed detector for the fast one. | `ZMART--object_analysis--cellpose`, `--classical` |
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

Name the environment in the step file, or override it in the recipe:

```python
METADATA = {"environment": "ZMART--object_analysis--cellpose", "max_workers": 1}

def run(pipeline_data, state, **params):
    from cellpose import models
    if "model" not in state:
        state["model"] = models.CellposeModel(gpu=params.get("gpu", False))
    ...
```

The engine reads `METADATA` without importing the file, so the heavy
imports happen only inside the worker, in the right environment.

## Steps over a field, a well, a plate

Give a step a `scope`, and it waits until the caller says that unit is
complete, then runs once over everything collected for it:

```yaml
overview:
  - detect_objects:
  - summarise_well:
      scope: well
  - summarise_plate:
      scope: plate
```

```python
engine.submit("overview", tile, scope={"well": "B3", "plate": "P1"})
...
engine.submit("overview", last_tile, scope={"well": "B3", "plate": "P1"}, complete="well")
```

The engine never guesses when a well is done. The acquisition knows, and
says so.

## Reading the results

`engine.results(name)` returns every finished job since you last asked,
each a dictionary with the step outputs under the step's name. Failures
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
