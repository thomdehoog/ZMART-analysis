"""
Test step -- verifies data from step_write_data survived serialization.

Checks that all values are intact after environment switching.
Reports pass/fail for each field.
"""

METADATA = {
    "description": "Verify test data after env switch",
    "version": "1.0",
    "environment": "local",
}


def run(pipeline_data: dict, **params) -> dict:
    import os
    import sys

    verbose = pipeline_data["metadata"].get("verbose", 0)

    if "step_write_data" not in pipeline_data:
        raise ValueError("step_write_data not found in pipeline_data")

    data = pipeline_data["step_write_data"]

    checks = {
        "string": data.get("string") == "hello_from_step_write_data",
        "integer": data.get("integer") == 42,
        "float": abs(data.get("float", 0) - 3.14159) < 1e-5,
        "boolean": data.get("boolean") is True,
        "list_length": len(data.get("list", [])) == 5,
        "list_mixed_types": data.get("list") == [1, 2, 3, "four", 5.0],
        "nested_key": data.get("nested", {}).get("key") == "value",
        "nested_numbers": data.get("nested", {}).get("numbers") == [10, 20, 30],
        "none_value": data.get("none_value") is None,
        "has_written_by_env": isinstance(data.get("written_by_env"), str),
        "has_written_by_pid": isinstance(data.get("written_by_pid"), int),
    }

    all_passed = all(checks.values())

    if verbose >= 2:
        print()
        print("=" * 50)
        print("  STEP: step_verify_data")
        print("=" * 50)
        for name, passed in checks.items():
            status = "PASS" if passed else "FAIL"
            print(f"    [{status}] {name}")
        print()
        if all_passed:
            print("    All checks passed")
        else:
            print("    SOME CHECKS FAILED")

    if not all_passed:
        failed = [k for k, v in checks.items() if not v]
        raise ValueError(f"Data verification failed: {failed}")

    pipeline_data["step_verify_data"] = {
        "all_passed": all_passed,
        "checks": checks,
        "verified_in_env": os.path.basename(sys.prefix),
        "verified_by_pid": os.getpid(),
    }

    return pipeline_data
