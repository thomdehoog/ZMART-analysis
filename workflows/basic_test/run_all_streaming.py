"""
Run all basic_test pipelines using the STREAMING engine and report results.

Mirrors run_all.py exactly, but uses StreamingEngine instead of run_pipeline.
Same tests, same environments, same expectations.

Usage:
    python run_all_streaming.py
    python run_all_streaming.py --keep-envs    # do not clean up after
    python run_all_streaming.py --skip-setup   # assume envs already exist
"""

import sys
import subprocess
import time
import argparse
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pyyaml", "-q"])

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from streaming import StreamingEngine, StepExecutionError

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
     "Step level environment switching"),

    ("pipeline_env",
     "pipelines/test_pipeline_env_pipeline.yaml",
     True,
     "Pipeline level environment switching"),

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


def run_script(script_path):
    """Run a Python script as a subprocess and return success status."""
    result = subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=False,
    )
    return result.returncode == 0


def main():
    parser = argparse.ArgumentParser(description="Run basic_test suite (streaming engine)")
    parser.add_argument(
        "--keep-envs", action="store_true",
        help="Do not clean up environments after tests",
    )
    parser.add_argument(
        "--skip-setup", action="store_true",
        help="Skip environment setup (assume envs exist)",
    )
    args = parser.parse_args()

    base = Path(__file__).parent
    setup_script = base / "environments" / "setup_env.py"
    clean_script = base / "environments" / "clean_env.py"
    t_start = time.time()

    print()
    print("=" * WIDTH)
    print("  SMART Analysis -- Basic Test Suite (Streaming Engine)")
    print("=" * WIDTH)

    print()
    print(f"  Tests:                 {len(TESTS)}")
    print(f"  Engine:                streaming")
    print()

    # ------------------------------------------------------------------
    # Setup environments
    # ------------------------------------------------------------------
    if not args.skip_setup:
        print("=" * WIDTH)
        print("  Phase 1: Environment Setup")
        print("=" * WIDTH)
        print()

        if not run_script(setup_script):
            print()
            print("  [FAIL]    Environment setup failed. Cannot run tests.")
            sys.exit(1)

        print()

    # ------------------------------------------------------------------
    # Run tests
    # ------------------------------------------------------------------
    print("=" * WIDTH)
    print("  Phase 2: Running Tests")
    print("=" * WIDTH)
    print()

    results = []

    with StreamingEngine(idle_timeout=60) as engine:
        for i, (name, yaml_file, should_pass, description) in enumerate(TESTS, 1):
            yaml_path = base / yaml_file

            print(f"  [{i:2d}/{len(TESTS)}] {name:<20s} {description}")

            passed = False
            error_msg = ""

            try:
                result = engine.process_file(
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

        # Show pool status before shutdown
        print(f"  Workers active:        {len(engine.pool.active_workers())}")
        print()

    # ------------------------------------------------------------------
    # Cleanup environments
    # ------------------------------------------------------------------
    if not args.keep_envs and not args.skip_setup:
        print("=" * WIDTH)
        print("  Phase 3: Cleanup")
        print("=" * WIDTH)
        print()

        run_script(clean_script)
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
    print(f"  {'_' * (WIDTH - 4)}")
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
