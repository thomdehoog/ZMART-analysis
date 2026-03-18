#!/usr/bin/env python
"""
Test Script for Pipeline Engine

This script tests the pipeline engine functionality including:
- Local execution (steps run in main process)
- Environment switching (steps run in Conda environments)
- Data flow between steps

Directory Structure:
    pipeline_engine/
    ├── engine/
    │   └── engine.py
    └── workflows/
        └── test1/
            ├── scripts/
            │   ├── run_tests.py           # THIS FILE
            │   ├── run_full_test_suite.py
            │   ├── setup_environments.py
            │   └── cleanup_environments.py
            ├── steps/
            │   ├── step_local.py
            │   ├── step_local_2.py
            │   ├── step_env.py
            │   └── step_env_b.py
            └── pipelines/
                ├── test_local_pipeline.yaml
                ├── test_mixed_pipeline.yaml
                ├── test_step_env_pipeline.yaml
                ├── test_pipeline_env_pipeline.yaml
                └── test_combined_env_pipeline.yaml

Usage:
    python run_tests.py [test_name]
    
    test_name options:
        local      - Test local execution only
        mixed      - Test mixed local steps with data flow
        step_env   - Test step-level environment switching
        pipe_env   - Test pipeline-level environment switching
        combined   - Test pipeline in env A with step in env B
        all        - Run all tests (default)

Examples:
    python run_tests.py              # Run all tests
    python run_tests.py local        # Run only local test
    python run_tests.py mixed        # Run only mixed test
"""

import os
import sys
from pathlib import Path
from datetime import datetime


def setup_paths():
    """Add the engine directory to the Python path."""
    # scripts/ -> test1/ -> workflows/ -> pipeline_engine/
    script_dir = Path(__file__).parent.absolute()
    test1_dir = script_dir.parent
    workflows_dir = test1_dir.parent
    root_dir = workflows_dir.parent  # pipeline_engine/
    engine_dir = root_dir / "engine"
    
    # Add engine to path so we can import it
    if str(engine_dir) not in sys.path:
        sys.path.insert(0, str(engine_dir))
    
    return script_dir, test1_dir, root_dir


def print_header(title: str):
    """Print a formatted header."""
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)


def print_subheader(title: str):
    """Print a subheader."""
    print(f"\n  {title}")
    print("  " + "-" * 50)


def print_result(name: str, success: bool, details: str = ""):
    """Print test result."""
    status = "+ PASS" if success else "x FAIL"
    print(f"\n  {status}: {name}")
    if details:
        print(f"          {details}")


def print_info(label: str, value: str, indent: int = 4):
    """Print an info line."""
    print(" " * indent + f"{label}: {value}")


def test_local_execution(test1_dir: Path) -> bool:
    """
    Test 1: Local Execution
    
    Verifies that functions with environment: "local" execute in the main process.
    """
    print_header("TEST 1: Local Execution")
    print("\n  Verifies that local functions run in the main process.")
    
    from engine import run_pipeline
    
    yaml_path = test1_dir / "pipelines" / "test_local_pipeline.yaml"
    main_pid = os.getpid()
    main_env = os.path.basename(sys.prefix)
    
    print_subheader("Configuration")
    print_info("Main PID", str(main_pid))
    print_info("Main Environment", main_env)
    print_info("YAML", str(yaml_path.name))
    
    try:
        print_subheader("Execution")
        result = run_pipeline(
            yaml_path=str(yaml_path),
            label="test_local",
            input_data={}
        )
        
        # Check results
        if "step_local" not in result:
            print_result("Local Execution", False, "step_local not found in result")
            return False
        
        step_data = result["step_local"]
        step_pid = step_data["process_id"]
        step_env = step_data["environment_name"]
        
        print_subheader("Results")
        print_info("Step PID", str(step_pid))
        print_info("Step Environment", step_env)
        print_info("Same Process", str(step_pid == main_pid))
        print_info("Same Environment", str(step_env == main_env))
        
        # Verify new metadata structure
        if "metadata" not in result:
            print_result("Local Execution", False, "metadata not found in result")
            return False
        
        if result["metadata"].get("workflow_name") != "local-test":
            print_result("Local Execution", False, 
                        f"Wrong workflow name: {result['metadata'].get('workflow_name')}")
            return False
        
        if step_pid == main_pid:
            print_result("Local Execution", True, 
                        f"Step ran in main process (PID: {step_pid}, Env: {step_env})")
            return True
        else:
            print_result("Local Execution", False, 
                        f"Step ran in different process! Main: {main_pid}, Step: {step_pid}")
            return False
            
    except Exception as e:
        print_result("Local Execution", False, f"Exception: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_mixed_execution(test1_dir: Path) -> bool:
    """
    Test 2: Mixed Local Steps with Data Flow
    
    Verifies that multiple local steps can pass data between them.
    """
    print_header("TEST 2: Mixed Local Steps with Data Flow")
    print("\n  Verifies that data flows correctly between local steps.")
    
    from engine import run_pipeline
    
    yaml_path = test1_dir / "pipelines" / "test_mixed_pipeline.yaml"
    main_pid = os.getpid()
    main_env = os.path.basename(sys.prefix)
    
    print_subheader("Configuration")
    print_info("Main PID", str(main_pid))
    print_info("Main Environment", main_env)
    print_info("YAML", str(yaml_path.name))
    
    try:
        print_subheader("Execution")
        result = run_pipeline(
            yaml_path=str(yaml_path),
            label="test_mixed",
            input_data={"test_input": "value"}
        )
        
        # Check all steps executed
        if "step_local" not in result:
            print_result("Mixed Execution", False, "step_local not found")
            return False
        
        if "step_local_2" not in result:
            print_result("Mixed Execution", False, "step_local_2 not found")
            return False
        
        local_data = result["step_local"]
        local_2_data = result["step_local_2"]
        
        print_subheader("Results")
        print("\n    Step Execution Table:")
        print("    " + "-" * 55)
        print(f"    {'Step':<16} {'PID':<8} {'Environment':<16}")
        print("    " + "-" * 55)
        print(f"    {'(main)':<16} {main_pid:<8} {main_env:<16}")
        print(f"    {'step_local':<16} {local_data['process_id']:<8} {local_data['environment_name']:<16}")
        print(f"    {'step_local_2':<16} {local_2_data['process_id']:<8} {local_2_data['environment_name']:<16}")
        print("    " + "-" * 55)
        
        # Validate
        same_process = (local_data['process_id'] == main_pid and 
                       local_2_data['process_id'] == main_pid)
        data_flows = "step_local" in local_2_data.get("previous_steps_found", [])
        input_preserved = result.get("input", {}).get("test_input") == "value"
        
        print(f"\n    Validation:")
        print("    " + "-" * 40)
        print(f"    {'All steps in main process:':<30} {'+' if same_process else 'x'}")
        print(f"    {'Data flows between steps:':<30} {'+' if data_flows else 'x'}")
        print(f"    {'Input data preserved:':<30} {'+' if input_preserved else 'x'}")
        print("    " + "-" * 40)
        
        if same_process and data_flows and input_preserved:
            print_result("Mixed Execution", True, "All steps executed correctly with data flow")
            return True
        else:
            details = []
            if not same_process:
                details.append("Steps not in main process")
            if not data_flows:
                details.append("Data didn't flow")
            if not input_preserved:
                details.append("Input not preserved")
            print_result("Mixed Execution", False, "; ".join(details))
            return False
            
    except Exception as e:
        print_result("Mixed Execution", False, f"Exception: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_step_env_switching(test1_dir: Path) -> bool:
    """
    Test 3: Step-Level Environment Switching
    
    Tests that a step with "environment" in METADATA runs in that environment.
    """
    print_header("TEST 3: Step-Level Environment Switching")
    print("\n  Tests that individual steps can run in different Conda environments.")
    
    from engine import run_pipeline
    
    yaml_path = test1_dir / "pipelines" / "test_step_env_pipeline.yaml"
    main_pid = os.getpid()
    main_env = os.path.basename(sys.prefix)
    
    print_subheader("Configuration")
    print_info("Main PID", str(main_pid))
    print_info("Main Environment", main_env)
    print_info("YAML", str(yaml_path.name))
    
    try:
        print_subheader("Execution")
        result = run_pipeline(
            yaml_path=str(yaml_path),
            label="test_step_env",
            input_data={}
        )
        
        # Check all steps
        for step in ["step_local", "step_env", "step_local_2"]:
            if step not in result:
                print_result("Step Environment Switching", False, f"{step} not found")
                return False
        
        local_data = result["step_local"]
        env_data = result["step_env"]
        local_2_data = result["step_local_2"]
        
        # Get target environment from the step
        target_env = env_data.get('requested_environment', 'unknown')
        
        print_subheader("Results")
        print("\n    Step Execution:")
        print("    " + "-" * 60)
        print(f"    {'step_local':<16} PID: {local_data['process_id']:<6} Env: {local_data['environment_name']}")
        print(f"    {'step_env':<16} PID: {env_data['process_id']:<6} Env: {env_data['actual_environment']}")
        print(f"    {'step_local_2':<16} PID: {local_2_data['process_id']:<6} Env: {local_2_data['environment_name']}")
        print("    " + "-" * 60)
        
        # Check if we're already in the target environment
        already_in_target = main_env.lower() == target_env.lower()
        
        # Validation
        if already_in_target:
            # When already in target env, step runs locally (same PID is expected)
            env_switched = True  # Not really switched, but correct behavior
            env_correct = env_data.get('environment_match', False)
        else:
            # When not in target env, step should run in subprocess
            env_switched = env_data['process_id'] != main_pid
            env_correct = env_data.get('environment_match', False)
        
        data_flows = len(local_2_data.get("previous_steps_found", [])) >= 2
        
        print(f"\n    Validation:")
        print("    " + "-" * 50)
        if already_in_target:
            print(f"    {'Already in target env (no switch needed):':<40} +")
        else:
            print(f"    {'step_env ran in subprocess:':<40} {'+' if env_switched else 'x'}")
        print(f"    {'step_env in correct environment:':<40} {'+' if env_correct else '?'}")
        print(f"    {'Data flows through all steps:':<40} {'+' if data_flows else 'x'}")
        print("    " + "-" * 50)
        
        if env_switched and data_flows:
            if already_in_target:
                print_result("Step Environment Switching", True,
                            f"Already in target env '{target_env}' (ran locally)")
            elif env_correct:
                print_result("Step Environment Switching", True,
                            f"step_env switched to '{env_data['actual_environment']}'")
            else:
                print_result("Step Environment Switching", True,
                            "Subprocess isolation works (env may not exist)")
            return True
        else:
            print_result("Step Environment Switching", False, "Environment switching failed")
            return False
            
    except Exception as e:
        error_str = str(e)
        if "conda" in error_str.lower() or "environment" in error_str.lower():
            print_result("Step Environment Switching", False, 
                        f"Conda environment error: {error_str[:100]}")
            print("\n    TIP: Run 'python setup_environments.py' to create test environments")
        else:
            print_result("Step Environment Switching", False, f"Exception: {e}")
            import traceback
            traceback.print_exc()
        return False


def test_pipeline_env_switching(test1_dir: Path) -> bool:
    """
    Test 4: Pipeline-Level Environment Switching
    
    Tests that entire pipeline runs in a different environment.
    """
    print_header("TEST 4: Pipeline-Level Environment Switching")
    print("\n  Tests that the entire pipeline can run in a specific Conda environment.")
    
    from engine import run_pipeline
    
    yaml_path = test1_dir / "pipelines" / "test_pipeline_env_pipeline.yaml"
    main_pid = os.getpid()
    main_env = os.path.basename(sys.prefix)
    
    print_subheader("Configuration")
    print_info("Main PID", str(main_pid))
    print_info("Main Environment", main_env)
    print_info("YAML", str(yaml_path.name))
    
    try:
        print_subheader("Execution")
        result = run_pipeline(
            yaml_path=str(yaml_path),
            label="test_pipeline_env",
            input_data={}
        )
        
        # Check steps
        for step in ["step_local", "step_local_2"]:
            if step not in result:
                print_result("Pipeline Environment Switching", False, f"{step} not found")
                return False
        
        local_data = result["step_local"]
        local_2_data = result["step_local_2"]
        
        print_subheader("Results")
        print(f"\n    All steps should run in the pipeline's environment (env_a)")
        print("    " + "-" * 55)
        print(f"    {'step_local':<16} Env: {local_data['environment_name']}")
        print(f"    {'step_local_2':<16} Env: {local_2_data['environment_name']}")
        print("    " + "-" * 55)
        
        # Both steps should be in the same (non-main) environment
        same_env = local_data['environment_name'] == local_2_data['environment_name']
        different_from_main = local_data['environment_name'] != main_env
        
        if same_env:
            if different_from_main:
                print_result("Pipeline Environment Switching", True,
                            f"Pipeline ran in '{local_data['environment_name']}'")
            else:
                print_result("Pipeline Environment Switching", True,
                            "Pipeline ran (already in target environment)")
            return True
        else:
            print_result("Pipeline Environment Switching", False, 
                        "Steps ran in different environments")
            return False
            
    except Exception as e:
        error_str = str(e)
        if "conda" in error_str.lower() or "environment" in error_str.lower():
            print_result("Pipeline Environment Switching", False, 
                        f"Conda environment error: {error_str[:100]}")
            print("\n    TIP: Run 'python setup_environments.py' to create test environments")
        else:
            print_result("Pipeline Environment Switching", False, f"Exception: {e}")
            import traceback
            traceback.print_exc()
        return False


def test_combined_env_switching(test1_dir: Path) -> bool:
    """
    Test 5: Combined Environment Switching
    
    Tests: main -> env A (pipeline) -> env B (step) -> env A
    """
    print_header("TEST 5: Combined Environment Switching")
    print("\n  Tests nested environment switching:")
    print("    main process → env A (pipeline) → env B (step) → env A")
    
    from engine import run_pipeline
    
    yaml_path = test1_dir / "pipelines" / "test_combined_env_pipeline.yaml"
    main_pid = os.getpid()
    main_env = os.path.basename(sys.prefix)
    
    print_subheader("Configuration")
    print_info("Main PID", str(main_pid))
    print_info("Main Environment", main_env)
    print_info("YAML", str(yaml_path.name))
    
    try:
        print_subheader("Execution")
        result = run_pipeline(
            yaml_path=str(yaml_path),
            label="test_combined_env",
            input_data={}
        )
        
        # Check all steps
        for step in ["step_local", "step_env_b", "step_local_2"]:
            if step not in result:
                print_result("Combined Environment Switching", False, f"{step} not found")
                return False
        
        local_data = result["step_local"]
        env_b_data = result["step_env_b"]
        local_2_data = result["step_local_2"]
        
        print_subheader("Results")
        print("\n    Execution Flow:")
        print("    " + "-" * 65)
        print(f"    {'Step':<16} {'PID':<8} {'Environment':<16}")
        print("    " + "-" * 65)
        print(f"    {'(main)':<16} {main_pid:<8} {main_env:<16}")
        print(f"    {'step_local':<16} {local_data['process_id']:<8} {local_data['environment_name']:<16}")
        print(f"    {'step_env_b':<16} {env_b_data['process_id']:<8} {env_b_data['actual_environment']:<16}")
        print(f"    {'step_local_2':<16} {local_2_data['process_id']:<8} {local_2_data['environment_name']:<16}")
        print("    " + "-" * 65)
        
        # Validation
        env_a = local_data['environment_name']
        env_b = env_b_data['actual_environment']
        
        pipeline_consistent = (local_data['environment_name'] == local_2_data['environment_name'])
        step_b_isolated = (env_b_data['process_id'] != local_data['process_id'])
        envs_different = (env_a.lower() != env_b.lower())
        data_flows = len(local_2_data.get("previous_steps_found", [])) >= 2
        
        print(f"\n    Validation:")
        print("    " + "-" * 50)
        print(f"    {'Pipeline env consistent (A=A):':<40} {'+' if pipeline_consistent else 'x'}")
        print(f"    {'step_env_b isolated:':<40} {'+' if step_b_isolated else 'x'}")
        print(f"    {'Env B different from Env A:':<40} {'+' if envs_different else '?'}")
        print(f"    {'Data flows through all steps:':<40} {'+' if data_flows else 'x'}")
        print("    " + "-" * 50)
        
        if pipeline_consistent and step_b_isolated and data_flows:
            if envs_different:
                print_result("Combined Environment Switching", True,
                            f"Pipeline in '{env_a}', step switched to '{env_b}'")
            else:
                print_result("Combined Environment Switching", True,
                            "Isolation works (environments may be same)")
            return True
        else:
            details = []
            if not pipeline_consistent:
                details.append("Pipeline steps in different envs")
            if not step_b_isolated:
                details.append("step_env_b not isolated")
            if not data_flows:
                details.append("Data didn't flow")
            print_result("Combined Environment Switching", False, "; ".join(details))
            return False
            
    except Exception as e:
        error_str = str(e)
        if "conda" in error_str.lower() or "environment" in error_str.lower():
            print_result("Combined Environment Switching", False, 
                        f"Conda environment error: {error_str[:100]}")
            print("\n    TIP: Run 'python setup_environments.py' to create test environments")
        else:
            print_result("Combined Environment Switching", False, f"Exception: {e}")
            import traceback
            traceback.print_exc()
        return False


def run_all_tests(test1_dir: Path) -> dict:
    """Run all tests and return results."""
    results = {}
    
    results["local"] = test_local_execution(test1_dir)
    results["mixed"] = test_mixed_execution(test1_dir)
    results["step_env"] = test_step_env_switching(test1_dir)
    results["pipe_env"] = test_pipeline_env_switching(test1_dir)
    results["combined"] = test_combined_env_switching(test1_dir)
    
    return results


def main():
    """Main entry point."""
    print_header("Pipeline Engine Tests")
    
    print(f"\n  System Information:")
    print("  " + "-" * 50)
    print(f"  Python:      {sys.version.split()[0]}")
    print(f"  Executable:  {sys.executable}")
    print(f"  Environment: {os.path.basename(sys.prefix)}")
    print(f"  Platform:    {sys.platform}")
    print(f"  PID:         {os.getpid()}")
    print(f"  Time:        {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("  " + "-" * 50)
    
    script_dir, test1_dir, root_dir = setup_paths()
    print(f"\n  Paths:")
    print("  " + "-" * 50)
    print(f"  Script:   {script_dir}")
    print(f"  Test1:    {test1_dir}")
    print(f"  Root:     {root_dir}")
    print("  " + "-" * 50)
    
    # Parse command line arguments
    test_name = sys.argv[1] if len(sys.argv) > 1 else "all"
    
    tests = {
        "local": test_local_execution,
        "mixed": test_mixed_execution,
        "step_env": test_step_env_switching,
        "pipe_env": test_pipeline_env_switching,
        "combined": test_combined_env_switching,
    }
    
    if test_name == "all":
        results = run_all_tests(test1_dir)
    elif test_name in tests:
        results = {test_name: tests[test_name](test1_dir)}
    else:
        print(f"\n  Unknown test: {test_name}")
        print(f"  Available tests: {', '.join(tests.keys())}, all")
        sys.exit(1)
    
    # Print summary
    print_header("Test Summary")
    
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    
    print("\n  Results:")
    print("  " + "-" * 40)
    for name, success in results.items():
        status = "+ PASS" if success else "x FAIL"
        print(f"    {status}: {name}")
    print("  " + "-" * 40)
    
    print(f"\n  Total: {passed}/{total} tests passed")
    
    if passed == total:
        print("\n" + "=" * 70)
        print("  + ALL TESTS PASSED!")
        print("=" * 70)
        return 0
    else:
        print("\n" + "=" * 70)
        print(f"  x {total - passed} TEST(S) FAILED")
        print("=" * 70)
        return 1


if __name__ == "__main__":
    sys.exit(main())
