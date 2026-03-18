# Smart Analysis

A Python pipeline engine for scientific analysis workflows. Define multi-step processing pipelines in YAML, and let the engine handle execution, data flow, and environment isolation.

## The Problem

Scientific analysis pipelines often combine tools with conflicting dependencies. A typical workflow might need scikit-image for preprocessing, PyTorch for deep learning, and specialized packages for feature extraction. These tools ship native libraries that can interfere with each other, leading to crashes that are hard to diagnose and harder to fix.

The traditional workaround is manual: run each tool in a separate script, save intermediate results to disk, and hope the file formats are compatible. This is fragile, hard to reproduce, and painful to modify.

## The Solution

Smart Analysis solves this with three ideas:

1. **YAML-defined pipelines** -- Each step is a simple Python function. The pipeline order and parameters are defined in YAML, not code. Changing your workflow means editing a config file, not rewriting a script.

2. **Automatic environment switching** -- Each step can declare which conda environment it needs. The engine handles subprocess spawning, data serialization, and result collection. Your preprocessing step can run in one environment while your segmentation runs in another, seamlessly.

3. **Shared data dictionary** -- A single `pipeline_data` dictionary flows through every step. Each step reads what it needs from previous steps and adds its own results. No manual file I/O between steps.

## How It Works

```
  YAML Config                    Engine                         Steps
  -----------                    ------                         -----

  rare-event-selection:     ->   run_pipeline()            ->   preprocess.py
    - preprocess:                  |                              |
        sigma: 1.0                 |  pipeline_data = {}          | reads image
    - segment:                     |  for step in steps:          | applies filters
        diameter: null             |      load step               | stores result
    - extract_features:            |      check environment       |
        percentile: 99             |      run step              segment.py
    - feedback:                    |      collect result           |
        output_dir: ./out          |  return pipeline_data        | runs Cellpose
                                                                  | stores masks
                                                                  |
                                                              extract_features.py
                                                                  |
                                                                  ...
```

## Quick Start

```bash
git clone https://github.com/thomdehoog/smart-analysis.git
cd smart-analysis
pip install pyyaml
```

### Writing a Step

Every step is a Python file with two things: a `METADATA` dict and a `run` function.

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

### Writing a Pipeline

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

### Running a Pipeline

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

## Environment Switching

This is the core feature. Scientific Python has a dependency conflict problem: packages like PyTorch, TensorFlow, and scipy ship native libraries (DLLs on Windows, .so on Linux) that can interfere with each other. The engine isolates steps in separate conda environments when needed.

### Three Levels of Isolation

**Level 1 -- Local (no isolation)**

Step runs in the main process. Fast, no overhead. Use when dependencies are compatible.

```python
METADATA = {"environment": "local"}
```

**Level 2 -- Pipeline-level environment**

All steps run together in one subprocess, in a different conda env than the orchestrator.

```yaml
metadata:
  environment: "SMART--my_workflow--main"
```

**Level 3 -- Step-level environment**

Individual steps get their own subprocess. Use when a specific step has unique dependencies.

```python
METADATA = {
    "environment": "SMART--my_workflow--segment",
    "data_transfer": "file_paths",  # or "pickle" for complex objects
}
```

### Environment Naming Convention

```
SMART--{workflow}--{step}

SMART--rare_event_selection--main       default env for the workflow
SMART--rare_event_selection--segment    isolated env for a specific step
SMART--basic_test--env_a                test environment A
```

### Environment Setup

Each workflow includes setup and cleanup scripts:

```bash
cd workflows/rare_event_selection/environments

# auto-detects GPU (CUDA/MPS/CPU), creates conda env, installs packages
python setup_env.py

# remove all envs for this workflow
python clean_env.py
```

The setup script auto-detects your GPU, picks the right PyTorch wheel, installs all packages via pip (avoiding conda/pip DLL conflicts), and runs diagnostics to verify everything works.

## Project Structure

```
smart-analysis/
|-- engine/
|   |-- engine.py              # pipeline orchestrator
|   |-- conda_utils.py         # conda discovery and GPU detection
|   '-- test_conda_utils.py    # unit tests (21 tests)
|-- workflows/
|   |-- basic_test/            # engine test suite (9 integration tests)
|   |   |-- environments/
|   |   |   |-- setup_env.py
|   |   |   '-- clean_env.py
|   |   |-- pipelines/         # 9 test pipelines
|   |   |-- steps/             # 8 test steps
|   |   '-- run_all.py
|   '-- rare_event_selection/  # example: microscopy cell analysis
|       |-- environments/
|       |   |-- setup_env.py
|       |   '-- clean_env.py
|       |-- pipelines/
|       |-- steps/
|       '-- run_pipeline.py
|-- docs/
|   '-- Pipeline_Engine_Documentation.md
|-- requirements.txt
|-- LICENSE
'-- .gitignore
```

## Testing

```bash
# set up test environments
cd workflows/basic_test/environments
python setup_env.py

# run all 9 tests
cd ..
python run_all.py

# clean up
cd environments
python clean_env.py
```

The test suite covers:
- Local step execution
- Data flow between steps
- Step-level environment switching
- Pipeline-level environment switching
- Nested environment switching
- Data survival across serialization
- Pickle transfer mode
- Error handling
- Missing step detection

## Requirements

- Python 3.10+
- PyYAML
- Conda (for environment switching)

## License

MIT License -- see LICENSE file for details.
