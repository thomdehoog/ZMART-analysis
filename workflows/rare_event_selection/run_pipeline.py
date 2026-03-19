"""
Run the rare_event_selection pipeline.

Usage:
    python run_pipeline.py
    python run_pipeline.py --label experiment_001
    python run_pipeline.py --source path/to/image.tif
"""

import sys
import argparse
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from engine import run_pipeline


YAML_PATH = Path(__file__).parent / "pipelines/rare_event_selection_pipeline.yaml"


def main():
    parser = argparse.ArgumentParser(
        description="Run the rare_event_selection pipeline"
    )
    parser.add_argument(
        "--label", default="run",
        help="Label for this run (default: run)",
    )
    parser.add_argument(
        "--source", default="skimage.human_mitosis",
        help="Data source: skimage.human_mitosis or path to image "
             "(default: skimage.human_mitosis)",
    )
    args = parser.parse_args()

    print(f"Running pipeline: rare_event_selection")
    print(f"YAML: {YAML_PATH}")
    print(f"Label: {args.label}")
    print(f"Source: {args.source}")
    print()

    result = run_pipeline(
        yaml_path=str(YAML_PATH),
        label=args.label,
        input_data={"data_source": args.source},
    )

    print()
    print("=" * 60)
    print("  Result")
    print("=" * 60)
    print(f"  Cells segmented:  {result['segment']['n_cells']}")
    print(f"  Cells selected:   {len(result['feedback']['cells'])}")
    print(f"  Feedback file:    {result['feedback']['filepath']}")
    print()
    for cell in result["feedback"]["cells"]:
        print(f"    label={cell['label']:3d}  "
              f"pos=({cell['centroid_x']:.1f}, {cell['centroid_y']:.1f})  "
              f"area={cell['area']}px")
    print()
    print("=" * 60)


if __name__ == "__main__":
    main()
