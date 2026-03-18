"""
Run all basic_test pipelines and report results.

Usage:
    python run_all.py

Requires SMART--basic_test--env_a/b/c environments to be set up.
Run environments/setup_env.py first.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "engine"))
from engine import run_pipeline

WIDTH = 70

TESTS = [
    # (name, yaml, should_pass, description)
    ("local",
     "pipelines/test_local_pipeline.yaml",
     True,
     "Single local step"),

    ("mixed",
     "pipelines/test_mixed_pipeline.yaml",
     True,
     "Data flow between local steps"),

    ("step_env",
     "pipelines/test_step_env_pipeline.yaml",
     True,
     "Step-level environment switching"),

    ("pipeline_env",
     "pipelines/test_pipeline_env_pipeline.yaml",
     True,
     "Pipeline-level environment switching"),

    ("combined",
     "pipelines/test_combined_env_pipeline.yaml",
     True,
     "Nested environment switching"),

    ("data_survival",
     "pipelines/test_data_survival_pipeline.yaml",
     True,
     "Data survives env switch serialization"),

    ("pickle",
     "pipelines/test_pickle_pipeline.yaml",
     True,
     "Pickle data transfer between envs"),

    ("error",
     "pipelines/test_error_pipeline.yaml",
     False,
     "Engine handles step errors gracefully"),

    ("missing_step",
     "pipelines/test_missing_step_pipeline.yaml",
     False,
     "Engine handles missing step files"),
]


def main():
    base = Path(__file__).parent
    t_start = time.time()

    print()
    print("=" * WIDTH)
    print("  SMART Analysis -- Basic Test Suite")
    print("=" * WIDTH)

    print()
    print(f"  Tests:                 {len(TESTS)}")
    print(f"  Engine:                {Path(__file__).parent.parent.parent / 'engine'}")
    print()

    results = []

    for i, (name, yaml_file, should_pass, description) in enumerate(TESTS, 1):
        yaml_path = base / yaml_file

        print(f"  [{i:2d}/{len(TESTS)}] {name:<20s} {description}")

        passed = False
        error_msg = ""

        try:
            result = run_pipeline(
                yaml_path=str(yaml_path),
                label=f"test_{name}",
                input_data={},
            )
            passed = True
        except Exception as e:
            error_msg = str(e)
            passed = False

        if should_pass:
            if passed:
                print(f"         [ OK ]    passed as expected")
            else:
                print(f"         [FAIL]    expected pass, got error:")
                print(f"                   {error_msg[:80]}")
        else:
            if not passed:
                print(f"         [ OK ]    failed as expected ({error_msg[:50]})")
            else:
                print(f"         [FAIL]    expected failure, but passed")

        correct = (passed == should_pass)
        results.append((name, correct))
        print()

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    elapsed = time.time() - t_start
    n_passed = sum(1 for _, ok in results if ok)
    n_total = len(results)
    all_ok = n_passed == n_total

    print("=" * WIDTH)
    print("  Test Results")
    print("=" * WIDTH)

    print()
    for name, correct in results:
        status = "[ OK ]" if correct else "[FAIL]"
        print(f"  {status}    {name}")

    print()
    print(f"  {'-' * (WIDTH - 4)}")
    print(f"  Passed:                {n_passed}/{n_total}")
    print(f"  Time:                  {elapsed:.0f}s")
    print()

    if all_ok:
        print("  ALL TESTS PASSED")
    else:
        print("  SOME TESTS FAILED")

    print()
    print("=" * WIDTH)

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
