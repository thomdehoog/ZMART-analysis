#!/usr/bin/env python
"""
Universal Test Suite Runner - Works with All Conda Versions

This version automatically detects the conda version and uses the appropriate
method to run tests, ensuring compatibility with both old and new conda versions.

Directory Structure:
    analysis_workflows/
    ├── engine/
    │   └── engine.py
    └── workflows/
        └── test1/
            ├── scripts/
            │   ├── run_tests.py
            │   ├── run_full_test_suite_universal.py  # THIS FILE
            │   ├── setup_environments.py
            │   └── cleanup_environments.py
            ├── steps/
            │   └── *.py
            └── pipelines/
                └── *.yaml

Supports:
  - Conda 4.x (old): Uses direct Python paths to avoid nested conda run bug
  - Conda 23.x+ (new): Uses conda run (faster, cleaner)
  - All platforms: Windows, macOS, Linux
  - All encodings: Automatically sets UTF-8 for subprocess output

Usage:
    python run_full_test_suite_universal.py              # Run all tests
    python run_full_test_suite_universal.py --env env_a  # Test from one env only
    python run_full_test_suite_universal.py --quick      # Run basic tests only

Requirements:
    - Conda environments set up (run setup_environments.py first)
    - Test files in place (analysis_workflows/ directory structure)
"""

import os
import sys
import subprocess
import json
import platform
import argparse
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Tuple, Optional


# Test configurations
# Note: "isolated" mode was removed in v2 - environment switching replaces it
TEST_CASES = ["local", "mixed", "step_env", "pipe_env", "combined"]
ENVIRONMENTS = ["env_a", "env_b", "env_c"]


def print_header(title, char="="):
    """Print a formatted header."""
    print(f"\n{char * 70}")
    print(f"  {title}")
    print(f"{char * 70}")


def get_script_dir() -> Path:
    """Get the scripts directory where this file resides."""
    return Path(__file__).parent.absolute()


def get_conda_version() -> Tuple[int, int, int]:
    """
    Get conda version as a tuple (major, minor, patch).
    Returns (0, 0, 0) if conda version cannot be determined.
    """
    try:
        result = subprocess.run(
            ["conda", "--version"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            # Parse "conda 4.12.0" or "conda 25.7.0"
            version_str = result.stdout.strip().split()[-1]
            parts = version_str.split('.')
            major = int(parts[0]) if len(parts) > 0 else 0
            minor = int(parts[1]) if len(parts) > 1 else 0
            patch = int(parts[2]) if len(parts) > 2 else 0
            return (major, minor, patch)
    except Exception as e:
        print(f"  Warning: Could not determine conda version: {e}")
    
    return (0, 0, 0)


def get_conda_info():
    """Get conda info."""
    try:
        result = subprocess.run(
            ["conda", "info", "--json"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except:
        pass
    return None


def find_env_python(env_name):
    """Find the Python executable for an environment."""
    conda_info = get_conda_info()
    if not conda_info:
        return None
    
    envs = conda_info.get("envs", [])
    for env_path in envs:
        if Path(env_path).name == env_name:
            if platform.system() == "Windows":
                python_exe = Path(env_path) / "python.exe"
            else:
                python_exe = Path(env_path) / "bin" / "python"
            
            if python_exe.exists():
                return str(python_exe)
    
    return None


def check_environments():
    """Check which environments exist and are available."""
    print("\nChecking environments...")
    
    available = {}
    for env_name in ENVIRONMENTS:
        python_exe = find_env_python(env_name)
        
        if python_exe:
            try:
                result = subprocess.run(
                    [python_exe, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                version = result.stdout.strip() or result.stderr.strip()
                print(f"  + {env_name}: {version}")
                available[env_name] = python_exe
            except Exception as e:
                print(f"  ! {env_name}: found but error: {e}")
        else:
            print(f"  x {env_name}: NOT FOUND")
    
    return available


def run_test_conda_run(test_name, env_name, scripts_dir, timeout=120):
    """
    Run test using conda run (for conda 23.0+).
    """
    import time
    
    cmd = ["conda", "run", "-n", env_name, "python", "run_tests.py", test_name]
    
    start_time = time.time()
    
    try:
        # Set UTF-8 encoding for subprocess output
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=str(scripts_dir)  # Run from scripts directory
        )
        
        elapsed = time.time() - start_time
        
        if result.returncode == 0:
            return True, elapsed, None
        else:
            error_msg = f"Exit code {result.returncode}"
            if result.stderr:
                error_msg += f"\n{result.stderr[:500]}"
            return False, elapsed, error_msg
    
    except subprocess.TimeoutExpired:
        elapsed = time.time() - start_time
        return False, elapsed, f"TIMEOUT after {timeout}s"
    except Exception as e:
        elapsed = time.time() - start_time
        return False, elapsed, str(e)


def run_test_direct_python(test_name, python_exe, scripts_dir, timeout=120):
    """
    Run test using direct Python path (for conda < 23.0).
    """
    import time
    
    cmd = [python_exe, "run_tests.py", test_name]
    
    start_time = time.time()
    
    try:
        # Set UTF-8 encoding for subprocess output
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=str(scripts_dir)  # Run from scripts directory
        )
        
        elapsed = time.time() - start_time
        
        if result.returncode == 0:
            return True, elapsed, None
        else:
            error_msg = f"Exit code {result.returncode}"
            if result.stderr:
                error_msg += f"\n{result.stderr[:500]}"
            return False, elapsed, error_msg
    
    except subprocess.TimeoutExpired:
        elapsed = time.time() - start_time
        return False, elapsed, f"TIMEOUT after {timeout}s"
    except Exception as e:
        elapsed = time.time() - start_time
        return False, elapsed, str(e)


def run_tests_from_env(env_name, python_exe, use_conda_run, scripts_dir, timeout=120, quick=False):
    """Run all tests from a specific environment."""
    print_header(f"Testing from: {env_name}", char="-")
    
    if use_conda_run:
        print("\n  Using: conda run (conda 23.0+ mode)")
    else:
        print("\n  Using: direct Python executable (conda < 23.0 compatible)")
        print(f"  Python: {python_exe}")
    
    # Target environments are now fixed in the step files:
    # - step_env.py -> env_b
    # - step_env_b.py -> env_c  
    # - test_pipeline_env_pipeline.yaml -> env_b
    # - test_combined_env_pipeline.yaml -> env_a (pipeline), env_c (step_env_b)
    targets = {"step_env": "env_b", "pipeline": "env_b", "step_env_b": "env_c"}
    
    print("\n  Target environments (from step/pipeline files):")
    print(f"    step_env.py        -> {targets['step_env']}")
    print(f"    step_env_b.py      -> {targets['step_env_b']}")
    print(f"    pipeline YAML      -> {targets['pipeline']}")
    
    results = {}
    timings = {}
    
    # Determine which tests to run
    if quick:
        tests_to_run = ["local", "mixed"]
        print("\n  Running basic tests only (--quick mode)...")
    else:
        tests_to_run = TEST_CASES
        print("\n  Running basic tests...")
    
    # Run basic tests
    for test in ["local", "isolated", "mixed"]:
        if test not in tests_to_run:
            continue
            
        print(f"    Running: {test}...", end=" ", flush=True)
        
        if use_conda_run:
            success, elapsed, error = run_test_conda_run(test, env_name, scripts_dir, timeout)
        else:
            success, elapsed, error = run_test_direct_python(test, python_exe, scripts_dir, timeout)
        
        results[test] = success
        timings[test] = elapsed
        
        if success:
            print(f"+ ({elapsed:.1f}s)")
        else:
            print(f"x ({elapsed:.1f}s)")
            if error:
                # Show first line of error
                first_line = error.split('\n')[0]
                print(f"      Error: {first_line}")
    
    # Run environment switching tests
    if not quick:
        print("\n  Running environment switching tests...")
        for test in ["step_env", "pipe_env", "combined"]:
            print(f"    Running: {test}...", end=" ", flush=True)
            
            if use_conda_run:
                success, elapsed, error = run_test_conda_run(test, env_name, scripts_dir, timeout)
            else:
                success, elapsed, error = run_test_direct_python(test, python_exe, scripts_dir, timeout)
            
            results[test] = success
            timings[test] = elapsed
            
            if success:
                print(f"+ ({elapsed:.1f}s)")
            else:
                print(f"x ({elapsed:.1f}s)")
                if error:
                    first_line = error.split('\n')[0]
                    print(f"      Error: {first_line}")
    
    # Summary
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    total_time = sum(timings.values())
    
    print(f"\n  Results for {env_name}: {passed}/{total} passed")
    print(f"  Total time: {total_time:.1f}s")
    
    return results, timings


def print_results_matrix(all_results):
    """Print a matrix of test results."""
    print("\n  Results Matrix:")
    print("  " + "-" * 60)
    
    # Determine which tests were run
    tests_run = []
    for results in all_results.values():
        tests_run = list(results.keys())
        break
    
    # Header
    header = "         "
    for test in tests_run:
        header += f" {test:10s}"
    print(header)
    
    # Results
    for env in ENVIRONMENTS:
        if env in all_results:
            row = f"  {env:7s}"
            for test in tests_run:
                result = all_results[env].get(test, False)
                symbol = "+" if result else "x"
                row += f"  {symbol:10s}"
            print(row)
    
    print("  " + "-" * 60)


def save_report(all_results, all_timings, total_elapsed, conda_version, use_conda_run, scripts_dir):
    """Save detailed test report to file."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = scripts_dir / f"test_results_{timestamp}.txt"
    
    with open(filename, 'w') as f:
        f.write("="*70 + "\n")
        f.write("  FULL TEST SUITE RESULTS\n")
        f.write("="*70 + "\n\n")
        f.write(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Conda version: {'.'.join(map(str, conda_version))}\n")
        f.write(f"Method: {'conda run' if use_conda_run else 'direct Python'}\n")
        f.write(f"Total elapsed time: {total_elapsed:.1f}s\n\n")
        
        # Results by environment
        for env in ENVIRONMENTS:
            if env in all_results:
                f.write(f"\n{env}:\n")
                f.write("-" * 40 + "\n")
                
                for test, result in all_results[env].items():
                    elapsed = all_timings[env].get(test, 0)
                    status = "PASS" if result else "FAIL"
                    f.write(f"  {test:15s}: {status:4s} ({elapsed:.1f}s)\n")
                
                passed = sum(1 for v in all_results[env].values() if v)
                total = len(all_results[env])
                f.write(f"\n  Summary: {passed}/{total} passed\n")
        
        # Overall stats
        total_tests = sum(len(results) for results in all_results.values())
        total_passed = sum(
            sum(1 for v in results.values() if v) 
            for results in all_results.values()
        )
        
        f.write("\n" + "="*70 + "\n")
        f.write(f"OVERALL: {total_passed}/{total_tests} passed ")
        f.write(f"({100*total_passed/total_tests:.0f}%)\n")
        f.write("="*70 + "\n")
    
    return filename


def main():
    """Main test runner."""
    parser = argparse.ArgumentParser(
        description="Universal test suite runner - works with all conda versions",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument(
        "--env",
        choices=ENVIRONMENTS,
        help="Run tests from only this environment"
    )
    
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Run only basic tests (local, isolated, mixed)"
    )
    
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Timeout per test in seconds (default: 120)"
    )
    
    parser.add_argument(
        "--force-direct",
        action="store_true",
        help="Force direct Python method even with newer conda"
    )
    
    args = parser.parse_args()
    
    # Get scripts directory
    scripts_dir = get_script_dir()
    
    print_header("Universal Test Suite - All Conda Versions")
    
    print(f"\n  Scripts directory: {scripts_dir}")
    
    # Detect conda version
    conda_version = get_conda_version()
    print(f"\n  Conda version: {'.'.join(map(str, conda_version))}")
    
    # Determine method to use
    use_conda_run = conda_version >= (23, 0, 0) and not args.force_direct
    
    if args.force_direct:
        print(f"  Method: Direct Python (forced)")
    elif use_conda_run:
        print(f"  Method: conda run (conda 23.0+ detected)")
    else:
        print(f"  Method: Direct Python (conda < 23.0 detected)")
    
    # Check environments
    available_envs = check_environments()
    
    if not available_envs:
        print("\nx No test environments found!")
        print("  Run: python setup_environments.py")
        return 1
    
    # Warn if not all environments available
    missing = set(ENVIRONMENTS) - set(available_envs.keys())
    if missing:
        print(f"\n! Warning: Missing environments: {', '.join(missing)}")
        print("  Tests will only run from available environments.")
    
    # Run tests
    import time
    start_time = time.time()
    all_results = {}
    all_timings = {}
    
    envs_to_test = [args.env] if args.env else ENVIRONMENTS
    
    for env_name in envs_to_test:
        if env_name in available_envs:
            python_exe = available_envs[env_name]
            results, timings = run_tests_from_env(
                env_name, 
                python_exe, 
                use_conda_run,
                scripts_dir,
                timeout=args.timeout,
                quick=args.quick
            )
            all_results[env_name] = results
            all_timings[env_name] = timings
        else:
            print(f"\n  Skipping {env_name} (not available)")
    
    total_elapsed = time.time() - start_time
    
    # Summary
    print_header("Test Summary")
    print_results_matrix(all_results)
    
    # Calculate overall stats
    total_tests = sum(len(results) for results in all_results.values())
    total_passed = sum(
        sum(1 for v in results.values() if v) 
        for results in all_results.values()
    )
    
    print(f"\n  Total: {total_passed}/{total_tests} passed ", end="")
    print(f"({100*total_passed/total_tests:.0f}%)")
    print(f"  Total elapsed time: {total_elapsed:.1f}s")
    
    # Save report
    report_file = save_report(all_results, all_timings, total_elapsed, conda_version, use_conda_run, scripts_dir)
    print(f"\n  Report saved: {report_file}")
    
    # Final status
    if total_passed == total_tests:
        print_header("+ ALL TESTS PASSED!")
        return 0
    else:
        print_header(f"x {total_tests - total_passed} TEST(S) FAILED")
        return 1


if __name__ == "__main__":
    sys.exit(main())
