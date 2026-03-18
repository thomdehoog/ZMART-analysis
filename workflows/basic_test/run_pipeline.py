"""
Run a basic_test pipeline.

Usage:
    python run_pipeline.py local
    python run_pipeline.py mixed
    python run_pipeline.py step_env
    python run_pipeline.py pipeline_env
    python run_pipeline.py combined
    python run_pipeline.py --list
"""

import sys
import json
from pathlib import Path

# Add engine to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "engine"))
from engine import run_pipeline


PIPELINES = {
    "local":        "pipelines/test_local_pipeline.yaml",
    "mixed":        "pipelines/test_mixed_pipeline.yaml",
    "step_env":     "pipelines/test_step_env_pipeline.yaml",
    "pipeline_env": "pipelines/test_pipeline_env_pipeline.yaml",
    "combined":     "pipelines/test_combined_env_pipeline.yaml",
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        sys.exit(0)

    if sys.argv[1] == "--list":
        print("Available pipelines:")
        for name, path in PIPELINES.items():
            print(f"  {name:<16s} {path}")
        sys.exit(0)

    name = sys.argv[1]
    if name not in PIPELINES:
        print(f"Unknown pipeline: '{name}'")
        print(f"Choose from: {', '.join(PIPELINES)}")
        sys.exit(1)

    yaml_path = Path(__file__).parent / PIPELINES[name]

    print(f"Running pipeline: {name}")
    print(f"YAML: {yaml_path}")
    print()

    result = run_pipeline(
        yaml_path=str(yaml_path),
        label=f"basic_test_{name}",
        input_data={},
    )

    print()
    print("=" * 60)
    print("  Result")
    print("=" * 60)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
