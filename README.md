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
4. **Analysis over a group, a compartment, a carrier.** Data is submitted
   while it is acquired. A step can be scoped so that it runs once a whole
   compartment is done, on everything collected for it, and a later step
   once the whole carrier is done. Per-object measurements, then a
   population per compartment, then the compartments of a carrier
   compared.

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
| `object_analysis/` | Which objects are in an image, and their size, shape, intensity and texture, as one table. Cellpose for the robust path, a watershed detector for the fast one. The scoped recipe adds a population per compartment and a comparison of the compartments per carrier. | `ZMART--object_analysis--cellpose`, `--classical` |
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

## Steps over a group, a compartment, a carrier

A sample is divided into four levels, narrowest first: a **tile**, a
**group** (a tile set), a **compartment** and a **carrier**. Each is a plain
number, so a compartment can be a well of a plate, a region of a slide or a
stretch of a cleared sample, and the same recipe serves them all.

Give a step a `scope`, and it waits until the caller says that unit is
complete, then runs once over everything collected for it. The scoped
recipe of object analysis, `object_analysis_scoped.yaml`, works this way:

```yaml
object_analysis:
  - detect_objects:           # every tile, as soon as it lands
  - extract_classical_features:
  - build_object_table:
  - summarise_population:     # once per compartment: its objects as a population
      scope: compartment
  - compare_populations:      # once per carrier: the compartments side by side
      scope: carrier
```

```python
engine.submit("scoped", tile, scope={"carrier": 1, "compartment": 3, "group": 2})
...
engine.submit("scoped", last_tile, scope={"carrier": 1, "compartment": 3},
              complete="compartment")
...
engine.submit("scoped", very_last_tile, scope={"carrier": 1, "compartment": 96},
              complete=["compartment", "carrier"])
```

The engine never guesses when a unit is done. The acquisition knows, and
says so. A unit is matched together with every wider level, because
numbers repeat: compartment 3 of carrier 1 never mixes with compartment 3
of carrier 2, so two carriers can be acquired at once.

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
