# Smart Analysis

A Python pipeline engine for scientific image analysis workflows. Define multi-step processing pipelines in YAML, and let the engine handle execution, data flow, and environment isolation.

## The Problem

Scientific analysis pipelines often combine tools with conflicting dependencies. A typical workflow might need scikit-image for preprocessing, PyTorch for deep learning, and specialized packages for feature extraction. These tools ship native libraries that can interfere with each other, leading to crashes that are hard to diagnose and harder to fix.

The common workarounds are either to find one environment that satisfies all dependencies, a time-consuming trial-and-error process that is not always possible, or to run each tool in a separate script, save intermediate results to disk, and stitch everything together manually. Both approaches are fragile, hard to reproduce, and painful to modify.

## The Solution

Smart Analysis solves this with three ideas:

1. **YAML defined pipelines.** Each step is a simple Python function. The pipeline order and parameters are defined in YAML, not code. Changing your workflow means editing a config file, not rewriting a script.

2. **Automatic environment switching.** Each step can declare which conda environment it needs. The engine handles subprocess spawning, data serialization, and result collection transparently.

3. **Shared data dictionary.** A single `pipeline_data` dictionary flows through every step. Each step reads what it needs from previous steps and adds its own results. No manual file I/O between steps.

## How It Works

You define your workflow in YAML. The engine reads it and executes each step in order, passing a shared data dictionary between them.

### Mode 1: All steps local (same process)

All steps share the same process and memory. Fast, no serialization overhead.

```
  ┌──────────────────────────────────────────────────────────────┐
  │    main process                                              │
  │                                                              │
  │    step 1 --> step 2 --> step 3                              │
  │                                                              │
  └──────────────────────────────────────────────────────────────┘
```

### Mode 2: Pipeline level environment (one subprocess)

All steps run together in a single subprocess, in a different conda env than the orchestrator. Useful when the entire workflow needs packages not available in the orchestrator env.

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

Individual steps get their own subprocess. The engine serializes pipeline_data between processes automatically. Use when a specific step has dependencies that conflict with other steps.

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

The pipeline runs in one env, but individual steps can switch to yet another env. The engine handles the nesting.

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

### YAML examples

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

## Quick Start

```bash
git clone https://github.com/thomdehoog/smart-analysis.git
cd smart-analysis
```

### Writing a step

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

## Environment switching

This is the core feature. Scientific Python has a dependency conflict problem. Packages like PyTorch, TensorFlow, and scipy ship native libraries that can interfere with each other. The engine isolates steps in separate conda environments when needed.

### Environment naming convention

```
SMART--{workflow}--{step}

SMART--rare_event_selection--main       default env for the workflow
SMART--rare_event_selection--segment    isolated env for a specific step
SMART--basic_test--env_a                test environment A
```

### Environment setup

Each workflow includes setup and cleanup scripts.

```bash
python workflows/rare_event_selection/environments/setup_env.py
python workflows/rare_event_selection/environments/clean_env.py
```

The setup script auto detects your GPU (NVIDIA CUDA, Apple MPS, or CPU), picks the right PyTorch build, installs all packages via pip (avoiding conda/pip DLL conflicts), and runs diagnostics to verify everything works.

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
            run_all.py

        rare_event_selection/  # example: microscopy cell analysis
            environments/
                setup_env.py
                clean_env.py
            pipelines/
            steps/
            run_pipeline.py

    docs/
        Pipeline_Engine_Documentation.md

    requirements.txt
    LICENSE
    .gitignore
```

## Testing

The test suite sets up environments, runs all tests, and cleans up automatically.

```bash
python workflows/basic_test/run_all.py
```

The test suite covers:

- Local step execution
- Data flow between steps
- Step level environment switching
- Pipeline level environment switching
- Nested environment switching
- Data survival across serialization
- Pickle transfer mode
- Error handling
- Missing step detection

## Requirements

- Python 3.10+
- PyYAML (auto installed by test suite if missing)
- Conda (for environment switching)

## License

MIT License. See LICENSE file for details.
