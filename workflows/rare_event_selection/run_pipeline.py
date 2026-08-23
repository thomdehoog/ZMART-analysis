"""
Run the rare_event_selection pipeline.

Usage:
    python run_pipeline.py
    python run_pipeline.py --label experiment_001
    python run_pipeline.py --source path/to/image.tif
    python run_pipeline.py --source path/to/position.zarr --label pos_A1

The source can be an OME-Zarr position (NGFF 0.4 or 0.5, one Zarr per
position), an OME-TIFF, a plain image file, or a skimage sample dataset.
Which plane is analysed is set by the level / t / c / z keys in the YAML,
and they mean the same thing for either format.
"""

import sys
import json
import argparse
from pathlib import Path

# Add engine to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "engine"))
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
        help="Data source: skimage.human_mitosis, an OME-Zarr position, "
             "an OME-TIFF, or a path to an image "
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
    image_metadata = result["preprocess"]["image_metadata"]

    print(f"  Image:            {image_metadata['format']} "
          f"{tuple(image_metadata['shape'])} "
          f"{''.join(image_metadata['axes'])}")
    if image_metadata["index"] or image_metadata["projection"]:
        plane = dict(image_metadata["index"])
        if image_metadata["channel"] is not None:
            plane["c"] = image_metadata["channel"]
        if image_metadata["projection"]:
            plane["z"] = image_metadata["projection"]
        print(f"  Plane:            {plane}")
    print(f"  Cells segmented:  {result['segment']['n_cells']}")
    print(f"  Cells selected:   {len(result['feedback']['cells'])}")
    print(f"  Feedback file:    {result['feedback']['filepath']}")
    print()
    for cell in result["feedback"]["cells"]:
        line = (f"    label={cell['label']:3d}  "
                f"pos=({cell['centroid_x']:.1f}, {cell['centroid_y']:.1f})px  "
                f"area={cell['area']}px")
        if "centroid_x_physical" in cell:
            line += (f"  ({cell['centroid_x_physical']:.1f}, "
                     f"{cell['centroid_y_physical']:.1f}) "
                     f"{cell['physical_unit']}")
        print(line)
    print()
    print("=" * 60)


if __name__ == "__main__":
    main()
