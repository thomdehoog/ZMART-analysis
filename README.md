# Smart Analysis

[![python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![dependencies](https://img.shields.io/badge/dependencies-PyYAML-brightgreen)](requirements.txt)
[![tests](https://img.shields.io/badge/tests-pytest-blue)](#testing)

Smart Analysis is the analysis part of **ZMART**, ZMB's Microscopy-Agnostic
Research Toolkit for smart microscopy, developed at the Center for Microscopy
and Image Analysis (ZMB), University of Zurich. It is a small pipeline engine
for scientific image analysis: you describe a multi-step analysis in a YAML
file, and the engine runs the steps in order, passes the data between them, and
gives each step the Python environment it needs.

## The Problem

Scientific analysis pipelines combine tools with conflicting dependencies. A
typical workflow needs scikit-image for preprocessing, PyTorch for deep
learning, and specialised packages for feature extraction. These tools ship
native libraries that interfere with each other, leading to crashes that are
hard to diagnose and harder to fix.

The usual workarounds are either to hunt for one environment that satisfies
every dependency, a trial-and-error process that is not always possible, or to
run each tool in its own script, save intermediate results to disk, and stitch
everything together by hand. Both are fragile, hard to reproduce, and painful to
change. For smart microscopy, where the analysis has to run reliably between
two acquisitions, that is not good enough.

## The Solution

Smart Analysis separates the analysis from the environment it runs in with
three ideas:

1. **Pipelines defined in YAML.** Each step is a plain Python function. The
   order of the steps and their parameters live in a YAML file, not in code.
   Changing your workflow means editing a config file, not rewriting a script.

2. **Automatic environment switching.** Each step can declare which conda
   environment it needs. The engine starts the subprocess, hands the data
   over, and collects the result. You never see any of that.

3. **One shared data dictionary.** A single `pipeline_data` dictionary flows
   through every step. Each step reads what it needs from the steps before it
   and adds its own results. There is no manual file handling between steps.

### The vocabulary

Everything you write and everything you call:

```python
# A step: one Python file with a METADATA dict and a run function
METADATA = {"description": "...", "version": "1.0", "environment": "local"}

def run(pipeline_data: dict, **params) -> dict:
    ...                          # read earlier results, add your own
    return pipeline_data
```

```yaml
# A pipeline: the steps in order, with their parameters
metadata:
  functions_dir: "../steps"

my-workflow:
  - preprocess: {sigma: 1.0}
  - segment:    {diameter: null}
```

```python
# Running it
from engine import run_pipeline

result = run_pipeline(yaml_path, label, input_data)
result["segment"]                # the output of any step, by its name
```

That is the whole surface: a step, a pipeline, and one call to run it.

## How It Works

You define your workflow in YAML. The engine reads it and executes each step in
order, passing the shared data dictionary between them. Where a step runs
depends on what it declares.

### Mode 1: All steps local (same process)

All steps share the same process and memory. Fast, with no serialisation.

```
  ┌──────────────────────────────────────────────────────────────┐
  │    main process                                              │
  │                                                              │
  │    step 1 --> step 2 --> step 3                              │
  │                                                              │
  └──────────────────────────────────────────────────────────────┘
```

### Mode 2: Pipeline level environment (one subprocess)

All steps run together in a single subprocess, in a different conda
environment than the one you started from. Useful when the whole workflow
needs packages you do not have in your main environment.

```
  ┌──────────────────────────────────────────────────────────────┐
  │   main process                                               │
  │                                                              │
  │  ┌────────────────────────────────────────────────────────┐  │
  │  │  subprocess                                            │  │
  │  │                                                        │  │
  │  │  step 1 --> step 2 --> step 3                          │  │
  │  │                                                        │  │
  │  └────────────────────────────────────────────────────────┘  │
  │                                                              │
  └──────────────────────────────────────────────────────────────┘
```

### Mode 3: Step level environment (per step subprocess)

One step gets its own subprocess. The engine serialises `pipeline_data` in and
out automatically. Use this when a single step has dependencies that conflict
with the others.

```
  ┌────────────────────────────────────────────────────────────────┐
  │  main process                                                  │
  │                                                                │
  │              ┌──────────────────┐                              │
  │              │ subprocess       │                              │
  │  step 1 -->  │     step 2       │ --> step 3                   │
  │              │                  │                              │
  │              └──────────────────┘                              │
  │                                                                │
  └────────────────────────────────────────────────────────────────┘
```

### Mode 4: Mixed (nested environments)

The pipeline runs in one environment, and a step inside it switches to yet
another. The engine handles the nesting.

```
  ┌──────────────────────────────────────────────────────────────┐
  │   main process                                               │
  │                                                              │
  │  ┌────────────────────────────────────────────────────────┐  │
  │  │  subprocess                                            │  │
  │  │                                                        │  │
  │  │              ┌──────────────────┐                      │  │
  │  │              │ subprocess       │                      │  │
  │  │  step 1 -->  │     step 2       │ --> step 3           │  │
  │  │              │                  │                      │  │
  │  │              └──────────────────┘                      │  │
  │  │                                                        │  │
  │  └────────────────────────────────────────────────────────┘  │
  │                                                              │
  └──────────────────────────────────────────────────────────────┘
```

### Choosing a mode

The mode is not a setting you pick; it follows from where you put the
`environment` key.

```yaml
# Mode 1: no environment key, everything runs locally
metadata:
  verbose: 3
  functions_dir: "../steps"

my-workflow:
  - preprocess:
      sigma: 1.0
  - segment:
      diameter: null
```

```yaml
# Mode 2: pipeline level environment
metadata:
  environment: "SMART--my_workflow--main"
  functions_dir: "../steps"

my-workflow:
  - preprocess:
      sigma: 1.0
  - segment:
      diameter: null
```

```python
# Mode 3: step level environment (in the step file)
METADATA = {
    "environment": "SMART--my_workflow--segment",
    "data_transfer": "file_paths",  # or "pickle" for complex objects
}
```

### What is no longer your problem

Because the engine is this simple, a whole class of problems stops being yours
as soon as you write against it:

- **In a step**, you never think about other steps' dependencies. You import
  what you need inside `run`, read from `pipeline_data`, and add your result.
- **In a pipeline**, you never write glue code. The order and the parameters
  are the YAML file, and swapping a step or a parameter is a one-line edit.
- **Between steps**, you never save and reload intermediate files by hand. The
  engine moves `pipeline_data` across process boundaries for you.

## Quick Start

### Install

```bash
git clone https://github.com/thomdehoog/smart-analysis.git
cd smart-analysis
```

The engine itself needs Python 3.10 or newer and PyYAML. Conda is needed only
when a step or a pipeline asks for its own environment.

### Writing a step

Every step is a Python file with two things: a `METADATA` dict and a `run`
function.

```python
# steps/my_step.py

METADATA = {
    "description": "What this step does",
    "version": "1.0",
    "environment": "local",       # or a conda env name
}

def run(pipeline_data: dict, **params) -> dict:
    # imports go inside run() to support environment switching
    import numpy as np

    # read from previous steps
    image = pipeline_data["preprocess"]["image"]

    # get parameters from YAML
    threshold = params.get("threshold", 0.5)

    # do work
    result = image > threshold

    # store output for next steps
    pipeline_data["my_step"] = {
        "result": result,
    }

    return pipeline_data
```

### Writing a pipeline

```yaml
# pipelines/my_pipeline.yaml

metadata:
  purpose: "Example workflow"
  version: "1.0"
  verbose: 3
  functions_dir: "../steps"

my-workflow:
  - preprocess:
      sigma: 1.0

  - my_step:
      threshold: 0.5

  - output:
      format: "csv"
```

### Running a pipeline

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path("engine")))
from engine import run_pipeline

result = run_pipeline(
    yaml_path="workflows/my_workflow/pipelines/my_pipeline.yaml",
    label="experiment_001",
    input_data={"data_source": "path/to/image.tif"},
)
```

`result` is the final `pipeline_data`: the run's `metadata`, the original
`input`, and one entry per step under the step's name.

### Setting up environments

Environments follow one naming convention, so a name tells you what it is for:

```
SMART--{workflow}--{step}

SMART--rare_event_selection--main       default env for the workflow
SMART--rare_event_selection--segment    isolated env for a specific step
SMART--basic_test--env_a                test environment A
```

Each workflow includes a setup and a cleanup script:

```bash
python workflows/rare_event_selection/environments/setup_env.py
python workflows/rare_event_selection/environments/clean_env.py
```

The setup script detects your GPU (NVIDIA CUDA, Apple MPS, or CPU), picks the
right PyTorch build, installs all packages through pip to avoid conda/pip
library conflicts, and runs diagnostics to check that everything works.

## Project structure

```
smart-analysis/
    engine/
        engine.py              # pipeline orchestrator
        conda_utils.py         # conda discovery and GPU detection
        test_conda_utils.py    # unit tests (21 tests)

    workflows/
        basic_test/            # engine test suite (9 integration tests)
            environments/
                setup_env.py
                clean_env.py
            pipelines/         # 9 test pipelines
            steps/             # 8 test steps
            test_engine.py     # the 9 pipelines as pytest tests
            run_all.py         # the same 9, as a printed report

        rare_event_selection/  # example: microscopy cell analysis
            environments/
                setup_env.py
                clean_env.py
            pipelines/
            steps/
            run_pipeline.py

    docs/
        Pipeline_Engine_Documentation.md

    pytest.ini
    requirements.txt
    LICENSE
    .gitignore
```

## Testing

One command from the repository root runs every test:

```bash
pip install pytest
pytest
```

It covers the engine's helpers (conda discovery, GPU detection) and nine
pipelines run through the real engine: local execution, data flow between
steps, step level and pipeline level environment switching, nested switching,
data survival across serialisation, pickle transfer, error handling, and
missing-step detection. The test environments are created before the first
test that needs them and removed afterwards; set `SMART_KEEP_ENVS=1` to keep
them between runs.

Without conda, the tests that need it are skipped rather than failed, so the
suite still gives an honest result. The older runner,
`python workflows/basic_test/run_all.py`, does the same nine pipelines with a
printed report.

## Status

This is a release candidate. The step format, the YAML layout and
`run_pipeline` are stable in spirit, and small changes may happen before 1.0.
If you build a workflow on it, please open an issue so we can keep the contract
honest together.

## Requirements

- Python 3.10+
- PyYAML (installed automatically by the test suite if missing)
- Conda, only for environment switching

## Author

Thom de Hoog, Center for Microscopy and Image Analysis (ZMB), University of
Zurich (thom.dehoog@zmb.uzh.ch, thomdehoog@gmail.com).

## License

MIT License. See LICENSE file for details.
