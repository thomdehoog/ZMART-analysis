# Pipeline Engine Documentation

A comprehensive guide to understanding, using, and extending the Pipeline Engine system.

---

## Table of Contents

1. [Introduction](#1-introduction)
2. [Core Concepts](#2-core-concepts)
3. [Architecture Overview](#3-architecture-overview)
4. [Getting Started](#4-getting-started)
5. [Writing Pipeline Functions](#5-writing-pipeline-functions)
6. [YAML Configuration](#6-yaml-configuration)
7. [Environment Switching](#7-environment-switching)
8. [Data Flow](#8-data-flow)
9. [Output Structure](#9-output-structure)
10. [Best Practices](#10-best-practices)
11. [Troubleshooting](#11-troubleshooting)
12. [API Reference](#12-api-reference)

---

## 1. Introduction

### What is the Pipeline Engine?

The Pipeline Engine is an **orchestration system** that executes a sequence of Python functions in a defined order. Think of it as an assembly line where each station (function) performs a specific task and passes the result to the next station.

```
┌──────────┐      ┌──────────┐      ┌──────────┐      ┌──────────┐
│  Step 1  │─────▶│  Step 2  │─────▶│  Step 3  │─────▶│  Step 4  │
│   Init   │      │ Process  │      │ Analyze  │      │  Output  │
└──────────┘      └──────────┘      └──────────┘      └──────────┘
      │                 │                 │                 │
      └─────────────────┴─────────────────┴─────────────────┘
                                    │
                              pipeline_data
                         (shared data dictionary)
```

### Why Use a Pipeline?

| Benefit | Description |
|---------|-------------|
| **Modularity** | Each function does one thing well |
| **Reusability** | Functions can be shared across pipelines |
| **Configurability** | Change behavior via YAML, no code changes |
| **Environment Switching** | Run steps in different Conda environments when needed |
| **Traceability** | Clear record of what ran and when |

### Key Features

- **YAML-based configuration** - Define pipelines without writing orchestration code
- **Environment switching** - Run steps in different Conda environments
- **Cross-platform** - Works on Windows, macOS, and Linux
- **Subprocess isolation** - Run steps in separate processes when needed
- **Flexible data passing** - Share data between steps via dictionary or file paths

---

## 2. Core Concepts

### 2.1 The Workflow

A **workflow** is an ordered sequence of functions that process data. Each workflow is defined in a YAML file and consists of:

- **Metadata** - Information about the workflow (purpose, version, settings)
- **Steps** - The functions to execute, in order
- **Parameters** - Configuration values passed to each function

The **workflow name** is defined by the YAML key (e.g., `rare-event-detection:`).

### 2.2 Pipeline Functions (Steps)

A **pipeline function** is a Python file containing:

1. **METADATA** - A dictionary describing the function
2. **run()** - The main function that does the work

```python
# Minimal pipeline function structure
METADATA = {
    "description": "What this function does",
    "version": "1.0"
}

def run(pipeline_data: dict, **params) -> dict:
    # Do work here
    return pipeline_data
```

### 2.3 Pipeline Data

**pipeline_data** is a dictionary that flows through all steps. Each step can:

- **Read** data added by previous steps
- **Add** new data for subsequent steps
- **Modify** existing data (use with caution)

```python
# Example pipeline_data structure after several steps
pipeline_data = {
    "metadata": {
        "datetime": "20260127-143052",
        "label": "experiment_001",
        "workflow_name": "rare-event-detection",
        "verbose": 2
    },
    "input": {...},                    # Original input
    "initialization": {...},           # Added by step 1
    "preprocessing": {...},            # Added by step 2
    "analysis_results": {...}          # Added by step 3
}
```

### 2.4 Environment Modes

The engine supports running steps in different Conda environments:

| Setting | Behavior |
|---------|----------|
| `environment: "local"` (or omitted) | Runs in current process |
| `environment: "env_name"` | Runs in subprocess using that Conda environment |

This can be set at two levels:
- **YAML metadata** - Applies to the entire workflow
- **Function METADATA** - Applies to a specific step

---

## 3. Architecture Overview

### 3.1 Directory Structure

```
smart-analysis/
├── engine/
│   └── engine.py                    # The orchestration engine
└── workflows/
    └── {workflow_folder}/           # e.g., rare-event-detection/
        ├── pipelines/
        │   └── {name}_pipeline.yaml # Workflow definition
        ├── steps/
        │   ├── step_one.py          # Step functions
        │   ├── step_two.py
        │   └── step_three.py
        └── scripts/                 # Utility scripts (optional)
            ├── setup_environments.py
            └── run_tests.py
```

**Convention:** The engine automatically looks for steps in a `steps/` folder that is a sibling to the `pipelines/` folder.

### 3.2 Execution Flow

```
                                    ┌─────────────────────┐
                                    │   run_pipeline()    │
                                    │    Entry Point      │
                                    └──────────┬──────────┘
                                               │
                              ┌────────────────┴────────────────┐
                              ▼                                 ▼
                    ┌──────────────────┐              ┌──────────────────┐
                    │  Pipeline Env    │              │ No Pipeline Env  │
                    │  Specified?      │              │  (Local Mode)    │
                    └────────┬─────────┘              └────────┬─────────┘
                             │ Yes                             │
                             ▼                                 │
                    ┌──────────────────┐                       │
                    │ Spawn Subprocess │                       │
                    │  in Target Env   │                       │
                    └────────┬─────────┘                       │
                             │                                 │
                             └───────────────┬─────────────────┘
                                             ▼
                                    ┌──────────────────┐
                              ┌────▶│  For Each Step   │◀────┐
                              │     └────────┬─────────┘     │
                              │              │               │
                              │              ▼               │
                              │     ┌──────────────────┐     │
                              │     │   Check Step     │     │
                              │     │   METADATA       │     │
                              │     └────────┬─────────┘     │
                              │              │               │
                    ┌─────────┴──────────────┼───────────────┘
                    │                        │
                    ▼                        ▼
          ┌─────────────────┐      ┌─────────────────┐
          │  Same Env or    │      │  Different Env  │
          │    "local"      │      │   Specified     │
          └────────┬────────┘      └────────┬────────┘
                   │                        │
                   ▼                        ▼
          ┌─────────────────┐      ┌─────────────────┐
          │    Run In       │      │   Spawn Step    │
          │    Current      │      │   Subprocess    │
          │    Process      │      │  in Target Env  │
          └────────┬────────┘      └────────┬────────┘
                   │                        │
                   └────────────┬───────────┘
                                │
                                ▼
                       ┌──────────────────┐
                       │  Return Result   │
                       └──────────────────┘
```

### 3.3 Component Responsibilities

**The engine handles:**
- YAML parsing
- Environment switching (spawning subprocesses when needed)
- Data serialization between environments
- Passing data between steps
- Error reporting

**The engine does NOT handle:**
- Output folder creation (steps do this)
- Step headers/formatting (steps do this)
- Image/plot display (steps do this)
- Detailed logging (steps do this)

---

## 4. Getting Started

### 4.1 Prerequisites

- Python 3.10 or higher
- PyYAML (`pip install pyyaml`)
- Conda (required for environment switching features)
- Conda version 23.0+ recommended (for cross-platform reliability)

### 4.2 Basic Usage

```python
from engine import run_pipeline

# Run a workflow
result = run_pipeline(
    yaml_path="workflows/my-workflow/pipelines/my_pipeline.yaml",
    label="experiment_001",
    input_data={"files": [...]}
)

# Access results
print(result["analysis"]["output_path"])
```

### 4.3 Your First Workflow

**Step 1: Create the folder structure**

```
smart-analysis/
└── workflows/
    └── demo/
        ├── pipelines/
        │   └── demo_pipeline.yaml
        └── steps/
            └── hello.py
```

**Step 2: Create a step function** (`steps/hello.py`)

```python
"""
Hello Step

A simple greeting function to demonstrate the pipeline.

IMPORTANT: All imports must be inside the run() function
to support environment switching.
"""

METADATA = {
    "description": "A simple greeting function",
    "version": "1.0"
}

def run(pipeline_data: dict, **params) -> dict:
    # Imports inside run()
    import os
    
    # Get parameters
    name = params.get("name", "World")
    verbose = pipeline_data["metadata"].get("verbose", 0)
    
    # Do work
    message = f"Hello, {name}!"
    
    if verbose >= 2:
        print()
        print("=" * 50)
        print("  STEP 1: hello")
        print("=" * 50)
        print(f"    Generated message: {message}")
    
    # Store output
    pipeline_data["hello"] = {
        "message": message,
        "params_used": {"name": name}
    }
    
    return pipeline_data
```

**Step 3: Create the YAML file** (`pipelines/demo_pipeline.yaml`)

```yaml
metadata:
  purpose: "Demo workflow"
  version: "1.0"
  verbose: 2

demo:
  - hello:
      name: "Pipeline User"
```

**Step 4: Run the workflow**

```python
from engine import run_pipeline

result = run_pipeline(
    yaml_path="workflows/demo/pipelines/demo_pipeline.yaml",
    label="my_first_run",
    input_data={}
)

print(result["hello"]["message"])
# Output: Hello, Pipeline User!
```

---

## 5. Writing Pipeline Functions

### 5.1 Function Structure

Every pipeline function follows this structure:

```python
"""
Function Name

Description of what this function does.
List responsibilities and expected inputs/outputs.

IMPORTANT: All imports must be inside the run() function
to support environment switching.
"""

##############
# metadata   #
##############
METADATA = {
    "description": "Brief description",
    "version": "1.0",
    "author": "Your Name",
    # Optional environment setting:
    # "environment": "env_name",     # Run in specific Conda env
    # "data_transfer": "file_paths", # How to pass data (default: "file_paths")
}

##############
# main       #
##############
def run(pipeline_data: dict, **params) -> dict:
    """
    Main function that performs the work.
    
    Parameters
    ----------
    pipeline_data : dict
        Shared data dictionary from previous steps.
    **params : dict
        Parameters from the YAML configuration.
    
    Returns
    -------
    dict
        Updated pipeline_data with this step's results.
    """
    # --- IMPORTS (inside run for environment switching support) ---
    import os
    import numpy as np
    
    # --- GET SETTINGS ---
    verbose = pipeline_data["metadata"].get("verbose", 0)
    
    # --- PRINT STEP HEADER (if verbose) ---
    if verbose >= 2:
        print()
        print("=" * 50)
        print("  STEP N: function_name")
        print("=" * 50)
    
    # --- MAIN LOGIC ---
    
    # 1. Read from pipeline_data
    input_data = pipeline_data["input"]
    
    # 2. Get parameters
    threshold = params.get("threshold", 0.5)
    
    # 3. Do work
    results = process(input_data, threshold)
    
    # 4. Add results to pipeline_data
    pipeline_data["my_step"] = {
        "output_path": "/path/to/outputs",
        "summary": {...},
        "params_used": {"threshold": threshold}
    }
    
    return pipeline_data
```

### 5.2 The METADATA Dictionary

| Key | Type | Required | Default | Description |
|-----|------|----------|---------|-------------|
| `description` | str | Yes | - | What the function does |
| `version` | str | Yes | - | Version number |
| `author` | str | No | - | Who wrote it |
| `environment` | str | No | `"local"` | `"local"` or Conda environment name |
| `data_transfer` | str | No | `"file_paths"` | `"pickle"` or `"file_paths"` (for cross-env steps) |

### 5.3 Why Imports Inside run()?

```python
# WRONG - imports at module level
import torch  # This runs when engine inspects METADATA!
from cellpose import models

METADATA = {"environment": "cellpose_env"}

def run(pipeline_data, **params):
    ...

# CORRECT - imports inside run()
METADATA = {"environment": "cellpose_env"}

def run(pipeline_data, **params):
    import torch  # Only runs when function executes
    from cellpose import models
    ...
```

**Why?** The engine imports your module to read METADATA. If you have imports at the top, they execute immediately - even if the function should run in a different environment where those packages are installed.

### 5.4 Accessing Previous Step Data

```python
def run(pipeline_data: dict, **params) -> dict:
    # Check if a previous step ran
    if "preprocessing" not in pipeline_data:
        raise ValueError(
            "This step requires 'preprocessing' to run first. "
            "Check your pipeline configuration."
        )
    
    # Access output from previous step
    preprocessed_path = pipeline_data["preprocessing"]["output_path"]
    
    # Access metadata
    label = pipeline_data["metadata"]["label"]
    workflow_name = pipeline_data["metadata"]["workflow_name"]
    
    return pipeline_data
```

### 5.5 Verbose Output

The verbose level is passed via `pipeline_data["metadata"]["verbose"]`:

| Level | Description |
|-------|-------------|
| 0 | Silent (only warnings and errors) |
| 1 | Engine messages only (before/after pipeline, summary) |
| 2 | Step messages only (each step prints its own output) |
| 3 | Both engine and step messages |

```python
def run(pipeline_data: dict, **params) -> dict:
    verbose = pipeline_data["metadata"].get("verbose", 0)
    
    # Step header (only shown at verbose >= 2)
    if verbose >= 2:
        print()
        print("=" * 50)
        print("  STEP 2: preprocessing")
        print("=" * 50)
        print()
        print("    #### Loading Data")
        print(f"    Found {len(files)} files")
    
    # ... do work ...
    
    if verbose >= 2:
        print()
        print("    #### Processing Complete")
        print(f"    Processed {count} items in {elapsed:.1f}s")
    
    return pipeline_data
```

**Note:** For displaying images and plots in interactive environments (Jupyter, VS Code), handle this within the step function. In terminal mode, save figures to disk and print the path.

---

## 6. YAML Configuration

### 6.1 Basic Structure

```yaml
# Workflow metadata
metadata:
  purpose: "Description of workflow"
  version: "1.0"
  author: "Your Name"
  verbose: 2                         # Verbosity level (0-3)
  # environment: "env_name"          # Run entire workflow in this env

# Workflow definition
# The key IS the workflow name
workflow-name:
  - step_one:
      param1: value1
      param2: value2
  
  - step_two:
      threshold: 0.5
      enabled: true
  
  - step_three:
      # No parameters needed
```

### 6.2 Workflow Naming

The **workflow name** is determined by the YAML key:

| YAML Key | Workflow Name | Output Folder |
|----------|---------------|---------------|
| `demo:` | demo | `{label}_demo_output/` |
| `rare-event-detection:` | rare-event-detection | `{label}_rare-event-detection_output/` |
| `cell-analysis:` | cell-analysis | `{label}_cell-analysis_output/` |

**Rule:** One workflow per YAML file.

### 6.3 Parameter Types

YAML supports various data types:

```yaml
my-workflow:
  - my_function:
      # String
      name: "experiment_001"
      
      # Number (integer)
      count: 42
      
      # Number (float)
      threshold: 0.75
      
      # Boolean
      enabled: true
      
      # List
      channels:
        - red
        - green
        - blue
      
      # Nested dictionary
      options:
        resize: true
        width: 512
        height: 512
```

### 6.4 Comments

Use `#` for comments to document your configuration:

```yaml
metadata:
  purpose: "Image analysis workflow"
  verbose: 2

cell-analysis:
  # Step 1: Initialize workspace
  # Creates output folders and parses input files
  - initialization:
      root_pattern: "(.*experiment.*)"
  
  # Step 2: Preprocess images
  # Applies normalization and filtering
  - preprocessing:
      normalize: true
      low_percentile: 1    # Saturate bottom 1%
      high_percentile: 99  # Saturate top 1%
```

---

## 7. Environment Switching

### 7.1 Why Environment Switching?

Different tools often have conflicting requirements:

| Tool | Python | PyTorch | Issue |
|------|--------|---------|-------|
| Cellpose | 3.8+ | 1.x | Specific CUDA version |
| New Library | 3.10+ | 2.x | Different CUDA version |
| Legacy Code | 3.7 | - | Old Python syntax |

**Solution:** Run each step in its own environment!

### 7.2 Environment Setting

The `environment` setting can be `"local"` or any Conda environment name:

#### In Function METADATA (step-level)

```python
# Runs in current process
METADATA = {
    "environment": "local"  # This is the default if omitted
}

# Runs in cellpose_env subprocess
METADATA = {
    "environment": "cellpose_env"
}
```

#### In YAML metadata (workflow-level)

```yaml
metadata:
  purpose: "Analysis requiring special environment"
  environment: "analysis_env"  # Whole workflow runs in this env

cell-analysis:
  - step_one:   # Runs in analysis_env
  - step_two:   # Runs in analysis_env
  - step_three: # Runs in analysis_env
```

### 7.3 Execution Scenarios

#### Scenario 1: All Local

```yaml
metadata:
  purpose: "Simple workflow"
  # No environment specified = local

simple-workflow:
  - step_one:   # local (in main process)
  - step_two:   # local (in main process)
```

```
┌────────────────────────────────────────────┐
│             Main Process                   │
│                                            │
│   ┌──────────┐       ┌──────────┐          │
│   │  Step 1  │       │  Step 2  │          │
│   │ (local)  │       │ (local)  │          │
│   └──────────┘       └──────────┘          │
│                                            │
└────────────────────────────────────────────┘
```

#### Scenario 2: Workflow in Specific Environment

```yaml
metadata:
  environment: "analysis_env"

analysis:
  - step_one:   # Runs in analysis_env
  - step_two:   # Runs in analysis_env
```

```
Main Process (your shell)
        │
        └───▶ Subprocess (analysis_env)
                    ┌──────────┐       ┌──────────┐
                    │  Step 1  │       │  Step 2  │
                    └──────────┘       └──────────┘
```

#### Scenario 3: Mixed Environments

```yaml
metadata:
  environment: "env_a"  # Workflow runs in env_a

mixed-workflow:
  - step_one:          # Runs in env_a (in process)
  - step_special:      # Has environment: "env_b" in METADATA
  - step_three:        # Runs in env_a (in process)
```

```
Main Process (your shell)
        │
        └───▶ Workflow Subprocess (env_a)
                    │
                    ├── step_one (env_a, in-process)
                    │
                    ├───▶ Step Subprocess (env_b)
                    │           └── step_special
                    │
                    └── step_three (env_a, in-process)
```

### 7.4 Smart Environment Detection

If a step specifies an environment that matches the current environment, it runs in-process (no subprocess):

```python
# If workflow is already running in "cellpose_env":
METADATA = {
    "environment": "cellpose_env"  # Same as current = runs in-process
}
```

### 7.5 Data Transfer Between Environments

When a step runs in a different environment, data must be serialized. Two modes are available:

| Mode | Setting | Use Case |
|------|---------|----------|
| File Paths | `"data_transfer": "file_paths"` (default) | Large data, numpy arrays |
| Pickle | `"data_transfer": "pickle"` | Small data, complex objects |

```python
METADATA = {
    "environment": "env_b",
    "data_transfer": "file_paths"  # Default - pass file paths, not data
}
```

**With `file_paths` mode:**
- Steps save outputs to disk
- `pipeline_data` contains paths, not actual data
- Recommended for large arrays, images, etc.

**With `pickle` mode:**
- Full `pipeline_data` is serialized via pickle
- Works for complex Python objects
- Be careful with large data (memory intensive)

---

## 8. Data Flow

### 8.1 The pipeline_data Dictionary

This is the central data structure that flows through all steps:

```python
pipeline_data = {
    # ================================================================
    # METADATA (set by engine)
    # ================================================================
    "metadata": {
        "datetime": "20260127-143052",      # Run timestamp
        "label": "experiment_001",           # User-provided label
        "workflow_name": "cell-analysis",    # From YAML key
        "yaml_filename": "cell_analysis_pipeline.yaml",
        "steps": ["init", "process", "analyze"],  # All step names
        "verbose": 2,                        # Verbosity level
        # ... other YAML metadata fields
    },
    
    # ================================================================
    # INPUT (provided by user)
    # ================================================================
    "input": {
        "data_path": "/path/to/data",
        "file_list": [...]
    },
    
    # ================================================================
    # STEP OUTPUTS (added by each step)
    # ================================================================
    "initialization": {
        "output_folder": "/path/to/output",
        "params_used": {...}
    },
    
    "preprocessing": {
        "output_path": "/path/to/preprocessed",
        "file_count": 42,
        "params_used": {...}
    },
    
    "analysis": {
        "results_path": "/path/to/results",
        "summary": {...},
        "params_used": {...}
    }
}
```

### 8.2 Step Output Convention

Each step should add its output under a key matching its name:

```python
# In preprocessing.py
def run(pipeline_data: dict, **params) -> dict:
    # ... do work ...
    
    # Add output under step name
    pipeline_data["preprocessing"] = {
        "output_path": output_folder,
        "file_count": len(files),
        "params_used": {
            "normalize": params.get("normalize", True)
        }
    }
    
    return pipeline_data
```

### 8.3 Best Practice: Use File Paths

For large data (images, arrays), store to disk and pass paths:

```python
def run(pipeline_data: dict, **params) -> dict:
    import numpy as np
    
    # Process data
    result_array = heavy_computation(...)
    
    # Save to disk
    output_path = "/path/to/output/results.npy"
    np.save(output_path, result_array)
    
    # Store PATH in pipeline_data, not the array itself
    pipeline_data["my_step"] = {
        "output_path": output_path,  # Path, not data
        "shape": result_array.shape,
        "dtype": str(result_array.dtype)
    }
    
    # Free memory
    del result_array
    
    return pipeline_data
```

### 8.4 Data Flow Diagram

```
run_pipeline(yaml_path, label, input_data)
                      │
                      ▼
      ┌───────────────────────────────────┐
      │          pipeline_data            │
      │   {                               │
      │     "metadata": {...},            │
      │     "input": input_data           │
      │   }                               │
      └─────────────────┬─────────────────┘
                        │
                        ▼
               ┌────────────────┐
               │     Step 1     │
               │ Initialization │
               └───────┬────────┘
                       │ adds "initialization": {...}
                       ▼
      ┌───────────────────────────────────┐
      │          pipeline_data            │
      │   {                               │
      │     "metadata": {...},            │
      │     "input": {...},               │
      │     "initialization": {...} ◀─NEW │
      │   }                               │
      └─────────────────┬─────────────────┘
                        │
                        ▼
               ┌────────────────┐
               │     Step 2     │
               │ Preprocessing  │
               └───────┬────────┘
                       │ adds "preprocessing": {...}
                       ▼
      ┌───────────────────────────────────┐
      │          pipeline_data            │
      │   {                               │
      │     "metadata": {...},            │
      │     "input": {...},               │
      │     "initialization": {...},      │
      │     "preprocessing": {...}  ◀─NEW │
      │   }                               │
      └─────────────────┬─────────────────┘
                        │
                        ▼
                  ... more steps ...
                        │
                        ▼
               ┌────────────────┐
               │     Return     │
               │ pipeline_data  │
               └────────────────┘
```

---

## 9. Output Structure

### 9.1 Output Folder Naming

The main output folder follows this convention:

```
{label}_{workflow_name}_output/
```

For example, calling:
```python
run_pipeline(
    yaml_path="...",
    label="experiment_001",
    input_data={...}
)
```

With workflow name `cell-analysis` produces:
```
experiment_001_cell-analysis_output/
```

### 9.2 Recommended Folder Structure

```
experiment_001_cell-analysis_output/
├── metadata.yaml                        # Overall run metadata
├── resources/                           # Models, configs for reproducibility
│   └── pixel_classifier.ilp
│
├── 01_initialization/
│   ├── step_metadata.yaml              # Step params, version, timing
│   └── ...
│
├── 02_preprocessing/
│   ├── step_metadata.yaml
│   └── normalized_images/
│       ├── pos001_normalized.tif
│       └── pos002_normalized.tif
│
├── 03_segmentation/
│   ├── step_metadata.yaml
│   ├── segmentation_masks/
│   │   ├── pos001_segmentation_mask.tif
│   │   └── pos002_segmentation_mask.tif
│   └── probability_maps/
│       ├── pos001_probability_map.tif
│       └── pos002_probability_map.tif
│
└── 04_analysis/
    ├── step_metadata.yaml
    └── measurements/
        ├── pos001_measurements.csv
        └── pos002_measurements.csv
```

### 9.3 Naming Conventions

**Step folders:**
```
{step_number}_{step_name}/
```
- `01_initialization/`
- `02_preprocessing/`
- `03_segmentation/`

Step numbers are determined by order in the YAML. If a step is reused, it gets a new number (e.g., `02_normalize` and `05_normalize`).

**Output type folders:**
```
{output_type}/
```
- `segmentation_masks/`
- `probability_maps/`
- `normalized_images/`

**File names:**
```
{position_basename}_{output_type}.{ext}
```
- `pos001_segmentation_mask.tif`
- `pos001_probability_map.tif`

### 9.4 Metadata Files

**Main metadata.yaml** (workflow level):
```yaml
label: "experiment_001"
workflow_name: "cell-analysis"
datetime: "20260127-143052"
yaml_file: "cell_analysis_pipeline.yaml"
engine_version: "1.0"
steps_executed:
  - initialization
  - preprocessing
  - segmentation
  - analysis
total_runtime_seconds: 245.3
```

**step_metadata.yaml** (per step):
```yaml
step_name: "segmentation"
step_number: 3
version: "1.0"
author: "Your Name"
environment: "cellpose_env"
start_time: "2026-01-27 14:32:15"
end_time: "2026-01-27 14:35:42"
runtime_seconds: 207.1
parameters_used:
  # Cellpose v4 uses CPSAM model (no model_type parameter)
  diameter: null  # auto-detect
input_files: 42
output_files: 42
```

### 9.5 Resources Folder

For reproducibility, store external resources (models, configs) in the `resources/` folder:

```
experiment_001_cell-analysis_output/
└── resources/
    ├── pixel_classifier.ilp       # ilastik classifier
    ├── cellpose_model.pth         # Custom model weights
    └── config.json                # Configuration files
```

Reference these in step_metadata.yaml for full reproducibility.

---

## 10. Best Practices

### 10.1 Function Design

#### Do One Thing Well

```python
# BAD: Function does too much
def run(pipeline_data, **params):
    # Load data
    # Preprocess
    # Segment
    # Extract features
    # Save results
    ...

# GOOD: Split into focused functions
# preprocessing.py - just preprocessing
# segmentation.py - just segmentation
# feature_extraction.py - just features
```

#### Always Record Parameters Used

```python
def run(pipeline_data: dict, **params) -> dict:
    threshold = params.get("threshold", 0.5)
    method = params.get("method", "default")
    
    # ... do work ...
    
    pipeline_data["my_step"] = {
        "output_path": output_path,
        "params_used": {          # Always include this!
            "threshold": threshold,
            "method": method
        }
    }
    return pipeline_data
```

#### Validate Inputs Early

```python
def run(pipeline_data: dict, **params) -> dict:
    # Check required previous steps
    if "preprocessing" not in pipeline_data:
        raise ValueError(
            "This step requires 'preprocessing' to run first. "
            "Check your pipeline configuration."
        )
    
    # Validate parameters
    threshold = params.get("threshold")
    if threshold is None:
        raise ValueError("Parameter 'threshold' is required")
    
    if not 0 <= threshold <= 1:
        raise ValueError(f"threshold must be 0-1, got {threshold}")
    
    # ... proceed with valid inputs ...
```

### 10.2 Memory Management

#### Save Intermediate Results to Disk

```python
def run(pipeline_data: dict, **params) -> dict:
    import numpy as np
    
    # Load and process
    large_array = load_and_process(...)
    
    # Save to disk immediately
    output_path = f"{output_folder}/result.npy"
    np.save(output_path, large_array)
    
    # Store path, not data
    pipeline_data["my_step"] = {
        "output_path": output_path
    }
    
    # Free memory
    del large_array
    import gc
    gc.collect()
    
    return pipeline_data
```

#### Process in Chunks

```python
def run(pipeline_data: dict, **params) -> dict:
    chunk_size = params.get("chunk_size", 100)
    
    for i in range(0, len(files), chunk_size):
        chunk = files[i:i+chunk_size]
        process_chunk(chunk)
        # Memory freed after each chunk
```

### 10.3 Verbose Output Format

Follow this convention for consistent output:

```python
def run(pipeline_data: dict, **params) -> dict:
    verbose = pipeline_data["metadata"].get("verbose", 0)
    
    if verbose >= 2:
        print()
        print("=" * 50)
        print("  STEP 3: segmentation")
        print("=" * 50)
        print()
        print("    #### Loading Model")
        print(f"    Model: {model_type}")
        print()
        print("    #### Processing Images")
        print(f"    Processing {len(files)} files...")
        print()
        print("    #### Complete")
        print(f"    Runtime: {elapsed:.1f}s")
    
    return pipeline_data
```

### 10.4 YAML Configuration

#### Use Descriptive Names

```yaml
# BAD
cell-analysis:
  - step1:
  - step2:
  - step3:

# GOOD
cell-analysis:
  - initialization:
  - preprocessing:
  - segmentation:
  - feature_extraction:
```

#### Document with Comments

```yaml
metadata:
  purpose: "Analyze cell morphology in microscopy images"
  version: "2.1"
  verbose: 2

cell-analysis:
  # Initialize workspace and validate inputs
  - initialization:
      root_pattern: "(.*experiment.*)"
  
  # Normalize intensity and remove background
  - preprocessing:
      normalize: true
      background_subtraction: true
      rolling_ball_radius: 50  # pixels
  
  # Segment cells using Cellpose
  - segmentation:
      # Cellpose v4 uses CPSAM model (no model_type parameter)
      diameter: null  # auto-detect
```

### 10.5 Environment Switching

#### Only Use When Necessary

Environment switching has overhead. Use it only when:
- Different Python version required
- Conflicting package dependencies
- GPU memory isolation needed

```python
# Only specify environment when actually needed
METADATA = {
    "description": "Cellpose segmentation",
    "version": "1.0",
    "environment": "cellpose_env"  # Only because cellpose needs specific PyTorch
}
```

#### Use file_paths for Large Data

```python
METADATA = {
    "environment": "analysis_env",
    "data_transfer": "file_paths"  # Don't pickle large arrays
}
```

---

## 11. Troubleshooting

### 11.1 Common Errors

#### "Environment not found"

```
Error: Conda environment 'cellpose_env' not found
```

**Cause:** The specified environment doesn't exist.

**Solution:**
```bash
# List available environments
conda env list

# Create the missing environment
conda create -n cellpose_env python=3.10
conda activate cellpose_env
pip install cellpose
```

#### "Step did not produce output"

```
RuntimeError: Step 'my_step' did not produce output
```

**Cause:** The subprocess crashed or didn't save results.

**Solution:** 
1. Check for errors in the step's output
2. Ensure the function returns `pipeline_data`
3. Check that all data is serializable (avoid lambdas, open file handles)

#### Import Errors in Environment Steps

```
ModuleNotFoundError: No module named 'special_package'
```

**Cause:** Package not installed in target environment.

**Solution:**
```bash
conda activate target_env
pip install special_package
```

#### Conda Version Warning

For reliable cross-platform environment switching, use Conda 23.0 or higher:

```bash
conda --version
# If below 23.0:
conda update conda
```

### 11.2 Debugging Tips

#### Check Environment

```python
def run(pipeline_data: dict, **params) -> dict:
    import sys
    import os
    
    print(f"[DEBUG] Python: {sys.executable}")
    print(f"[DEBUG] Environment: {os.path.basename(sys.prefix)}")
    print(f"[DEBUG] PID: {os.getpid()}")
    print(f"[DEBUG] Params: {params}")
    
    # ... rest of function
```

#### Inspect pipeline_data Structure

```python
def run(pipeline_data: dict, **params) -> dict:
    def print_structure(d, indent=0):
        for key, value in d.items():
            vtype = type(value).__name__
            print(" " * indent + f"{key}: {vtype}")
            if isinstance(value, dict) and indent < 4:
                print_structure(value, indent + 2)
    
    print("[DEBUG] pipeline_data structure:")
    print_structure(pipeline_data)
```

### 11.3 Performance Issues

#### Slow Subprocess Startup

**Problem:** Each environment-switched step takes seconds to start.

**Solutions:**
1. Combine related steps into one function
2. Only use environment switching when necessary
3. Keep the number of environment switches minimal

#### Memory Issues

**Problem:** Pipeline runs out of memory.

**Solutions:**
1. Save intermediate results to disk (see Best Practices)
2. Process data in chunks
3. Use `del` and `gc.collect()` to free memory
4. Use environment switching to isolate memory-heavy steps

---

## 12. API Reference

### 12.1 run_pipeline()

```python
def run_pipeline(yaml_path: str, label: str, input_data: dict) -> dict:
    """
    Run a complete workflow from a YAML configuration file.
    
    Parameters
    ----------
    yaml_path : str
        Path to the YAML configuration file.
    label : str
        Human-readable label for this run. Used in output folder names.
        Example: "experiment_001", "test_run_20260127"
    input_data : dict
        Input data to process. Should be a dictionary with informative keys.
        Example: {"data_path": "/path/to/data", "file_list": [...]}
        Can be empty dict {} if first step generates/selects data.
    
    Returns
    -------
    dict
        The final pipeline_data dictionary containing:
        - "metadata": Run information (datetime, label, workflow_name, etc.)
        - "input": Original input data
        - "{step_name}": Output from each step
    
    Raises
    ------
    FileNotFoundError
        If YAML file or step files don't exist.
    ValueError
        If YAML is malformed or missing required fields.
    RuntimeError
        If a step fails during execution.
    
    Examples
    --------
    >>> result = run_pipeline(
    ...     yaml_path="workflows/analysis/pipelines/analysis_pipeline.yaml",
    ...     label="experiment_001",
    ...     input_data={"data_path": "/path/to/images"}
    ... )
    >>> print(result["segmentation"]["output_path"])
    """
```

### 12.2 METADATA Dictionary

```python
METADATA = {
    # Required
    "description": str,      # What the function does
    "version": str,          # Version number (e.g., "1.0")
    
    # Optional
    "author": str,           # Who wrote it
    "environment": str,      # "local" (default) or Conda environment name
    "data_transfer": str,    # "file_paths" (default) or "pickle"
}
```

### 12.3 run() Function Signature

```python
def run(pipeline_data: dict, **params) -> dict:
    """
    Main function that performs the step's work.
    
    Parameters
    ----------
    pipeline_data : dict
        Shared data dictionary. Contains:
        - "metadata": dict with datetime, label, workflow_name, verbose, etc.
        - "input": Original input data
        - "{previous_step}": Output from previous steps
    
    **params : dict
        Parameters from the YAML configuration.
        Access with: params.get("name", default_value)
    
    Returns
    -------
    dict
        The pipeline_data dictionary with this step's output added.
        Convention: Add output under pipeline_data["{step_name}"]
    
    Raises
    ------
    Any exception will cause the pipeline to stop and report the error.
    """
```

### 12.4 pipeline_data Structure

```python
{
    "metadata": {
        "datetime": str,          # "YYYYMMDD-HHMMSS"
        "label": str,             # User-provided label
        "workflow_name": str,     # From YAML key
        "yaml_filename": str,     # YAML file name
        "steps": List[str],       # All step names in order
        "verbose": int,           # 0-3
        # ... other YAML metadata fields
    },
    "input": dict,                # Original input_data
    "{step_name}": {              # Output from each step
        "output_path": str,       # Path to outputs
        "params_used": dict,      # Parameters used
        # ... step-specific output
    }
}
```

---

## Quick Reference Card

### Directory Structure

```
smart-analysis/
├── engine/
│   └── engine.py
└── workflows/
    └── {workflow}/
        ├── pipelines/
        │   └── {name}_pipeline.yaml
        ├── steps/
        │   └── {step}.py
        └── scripts/
```

### YAML Template

```yaml
metadata:
  purpose: "Description"
  version: "1.0"
  verbose: 2
  # environment: "env_name"  # Optional

workflow-name:
  - step_one:
      param: value
  - step_two:
      param: value
```

### Function Template

```python
METADATA = {
    "description": "What it does",
    "version": "1.0",
    # "environment": "env_name",
    # "data_transfer": "file_paths",
}

def run(pipeline_data: dict, **params) -> dict:
    # Imports inside run()
    import numpy as np
    
    # Get settings
    verbose = pipeline_data["metadata"].get("verbose", 0)
    
    # Get params
    value = params.get("param", default)
    
    # Do work
    result = process(...)
    
    # Store output (paths, not data)
    pipeline_data["step_name"] = {
        "output_path": result_path,
        "params_used": {"param": value}
    }
    
    return pipeline_data
```

### Environment Options

| Location | Setting | Effect |
|----------|---------|--------|
| YAML metadata | `environment: "local"` | Workflow in current process |
| YAML metadata | `environment: "env_a"` | Workflow in env_a subprocess |
| Function METADATA | `environment: "local"` | Step in current process |
| Function METADATA | `environment: "env_b"` | Step in env_b subprocess |

### Verbose Levels

| Level | Engine | Steps |
|-------|--------|-------|
| 0 | Silent | Silent |
| 1 | Yes | No |
| 2 | No | Yes |
| 3 | Yes | Yes |

Warnings and errors are always shown.

---

*Documentation version 2.0 - Last updated March 2026*