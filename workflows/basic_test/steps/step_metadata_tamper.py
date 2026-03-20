"""
Test step -- modifies pipeline_data["metadata"] to test engine resilience.

Overwrites the label, adds extra keys, and verifies the engine continues
to function correctly even when a step mutates metadata.
"""

METADATA = {
    "description": "Tamper with metadata to test resilience",
    "version": "1.0",
    "environment": "local",
}


def run(pipeline_data: dict, **params) -> dict:
    import os
    import sys

    verbose = pipeline_data["metadata"].get("verbose", 0)

    original_label = pipeline_data["metadata"].get("label", "")

    # Tamper: overwrite the label
    pipeline_data["metadata"]["label"] = "TAMPERED"

    # Tamper: add extra keys
    pipeline_data["metadata"]["injected_key"] = "injected_value"
    pipeline_data["metadata"]["injected_number"] = 999

    if verbose >= 2:
        print()
        print("=" * 50)
        print("  STEP: step_metadata_tamper")
        print("=" * 50)
        print(f"    Original label: {original_label}")
        print(f"    New label: TAMPERED")
        print(f"    Added injected_key and injected_number")

    pipeline_data["step_metadata_tamper"] = {
        "executed": True,
        "original_label": original_label,
        "tampered_label": "TAMPERED",
        "environment": os.path.basename(sys.prefix),
        "process_id": os.getpid(),
    }

    return pipeline_data
