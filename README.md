# Pipeline Engine

A Python orchestration engine that executes sequences of functions defined in YAML configuration files, with support for Conda environment switching.

## Features

- **YAML-based configuration** - Define workflows without writing orchestration code
- **Environment switching** - Run steps in different Conda environments automatically
- **Flexible data passing** - Share data between steps via dictionary or file paths
- **Cross-platform** - Works on Windows, macOS, and Linux
- **Verbose logging** - Configurable output levels for debugging

## Quick Start

### Installation

```bash
# Clone the repository
git clone https://github.com/yourusername/pipeline-engine.git
cd pipeline-engine

# Install dependencies
pip install pyyaml
```

### Basic Usage

```python
from engine.engine import run_pipeline

result = run_pipeline(
    yaml_path="workflows/my-workflow/pipelines/pipeline.yaml",
    label="experiment_001",
    input_data={"data_path": "/path/to/data"}
)

print(result["my_step"]["output_path"])
```

### Your First Workflow

**1. Create a step function** (`workflows/demo/steps/hello.py`):

```python
METADATA = {
    "description": "A greeting function",
    "version": "1.0",
    "environment": "local"
}

def run(pipeline_data: dict, **params) -> dict:
    name = params.get("name", "World")
    
    pipeline_data["hello"] = {
        "message": f"Hello, {name}!"
    }
    
    return pipeline_data
```

**2. Create a pipeline YAML** (`workflows/demo/pipelines/demo_pipeline.yaml`):

```yaml
metadata:
  purpose: "Demo workflow"
  version: "1.0"
  verbose: 2

demo:
  - hello:
      name: "Pipeline User"
```

**3. Run the workflow**:

```python
from engine.engine import run_pipeline

result = run_pipeline(
    yaml_path="workflows/demo/pipelines/demo_pipeline.yaml",
    label="my_first_run",
    input_data={}
)

print(result["hello"]["message"])
# Output: Hello, Pipeline User!
```

## Directory Structure

```
pipeline_engine/
├── engine/
│   └── engine.py              # The orchestration engine
├── workflows/
│   └── {workflow_name}/
│       ├── pipelines/
│       │   └── {name}_pipeline.yaml
│       └── steps/
│           └── {step}.py
└── docs/
    └── Pipeline_Engine_Documentation.md
```

## Environment Switching

Steps can run in different Conda environments:

```python
# In your step file
METADATA = {
    "description": "Runs in cellpose environment",
    "version": "1.0",
    "environment": "cellpose_env"  # Specify Conda environment
}
```

Or set at the pipeline level:

```yaml
metadata:
  environment: "analysis_env"  # All steps run in this env

my-workflow:
  - step_one:
  - step_two:
```

## Documentation

See [docs/Pipeline_Engine_Documentation.md](docs/Pipeline_Engine_Documentation.md) for comprehensive documentation including:

- Writing pipeline functions
- YAML configuration options
- Environment switching details
- Data flow patterns
- Best practices
- Troubleshooting

## Testing

```bash
# Set up test environments (requires Conda)
cd workflows/test1/scripts
python setup_environments.py

# Run tests
python run_tests.py all

# Run specific test
python run_tests.py local
python run_tests.py mixed
```

## Requirements

- Python 3.10+
- PyYAML
- Conda (for environment switching features)

## License

MIT License - see LICENSE file for details.
