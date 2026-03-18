#!/usr/bin/env python
"""
Setup Test Environments

This script creates three Conda environments for testing the pipeline
environment switching functionality:
  - env_a: Python 3.10
  - env_b: Python 3.11  
  - env_c: Python 3.12

Each environment is minimal but has the required dependencies (pyyaml).

Directory Structure:
    analysis_workflows/
    ├── engine/
    │   └── engine.py
    └── workflows/
        └── test1/
            ├── scripts/
            │   ├── setup_environments.py   # THIS FILE
            │   └── ...
            ├── steps/
            └── pipelines/

Usage:
    python setup_environments.py          # Create all environments
    python setup_environments.py --clean  # Remove all test environments
    python setup_environments.py --check  # Check if environments exist

Requirements:
    - Conda or Miniconda installed
    - Run from a conda-enabled terminal (Anaconda Prompt, Miniconda Prompt, 
      or a terminal where 'conda' command works)

Cross-platform: Works on Windows, macOS, and Linux.
"""

import os
import sys
import subprocess
import argparse
import platform
import json
from pathlib import Path


# Environment definitions
ENVIRONMENTS = {
    "env_a": {
        "python_version": "3.10",
        "packages": ["pyyaml"],
        "description": "Test environment A (Python 3.10)"
    },
    "env_b": {
        "python_version": "3.11", 
        "packages": ["pyyaml"],
        "description": "Test environment B (Python 3.11)"
    },
    "env_c": {
        "python_version": "3.12",
        "packages": ["pyyaml"],
        "description": "Test environment C (Python 3.12)"
    }
}


def print_header(title: str):
    """Print a formatted header."""
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def print_step(step: str):
    """Print a step indicator."""
    print(f"\n-> {step}")


def run_command(cmd: list, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    """Run a command and optionally capture output."""
    print(f"  $ {' '.join(cmd)}")
    
    if capture:
        result = subprocess.run(cmd, capture_output=True, text=True)
    else:
        result = subprocess.run(cmd)
    
    if check and result.returncode != 0:
        if capture:
            print(f"  Error: {result.stderr}")
        raise RuntimeError(f"Command failed with exit code {result.returncode}")
    
    return result


def get_conda_info() -> dict:
    """
    Get all conda configuration info using 'conda info --json'.
    
    This is the single source of truth for:
    - conda executable location
    - environment directories  
    - root prefix
    - platform info
    
    Returns:
        dict: Parsed JSON output from 'conda info --json'
        
    Raises:
        FileNotFoundError: If conda is not available
    """
    try:
        result = subprocess.run(
            ["conda", "info", "--json"],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except FileNotFoundError:
        pass
    
    raise FileNotFoundError(
        "Could not run 'conda info'. Please ensure:\n"
        "  - Conda is installed\n"
        "  - You are running from a conda-enabled terminal\n"
        "    (Anaconda Prompt, Miniconda Prompt, or terminal with conda init)"
    )


def get_conda_executable(conda_info: dict) -> str:
    """
    Get the conda executable path from conda info.
    
    Parameters
    ----------
    conda_info : dict
        Output from get_conda_info()
        
    Returns
    -------
    str
        Path to conda executable, or 'conda' if it's in PATH
    """
    # conda_exe is available in conda info
    conda_exe = conda_info.get("conda_exe")
    if conda_exe and Path(conda_exe).exists():
        return conda_exe
    
    # Fallback: construct from root_prefix
    root_prefix = conda_info.get("root_prefix", "")
    if root_prefix:
        if platform.system() == "Windows":
            candidate = Path(root_prefix) / "Scripts" / "conda.exe"
        else:
            candidate = Path(root_prefix) / "bin" / "conda"
        if candidate.exists():
            return str(candidate)
    
    # If all else fails, assume it's in PATH
    return "conda"


def get_envs_dirs(conda_info: dict) -> list:
    """
    Get the list of environment directories from conda info.
    
    Parameters
    ----------
    conda_info : dict
        Output from get_conda_info()
        
    Returns
    -------
    list
        List of paths where conda environments are stored
    """
    return conda_info.get("envs_dirs", [])


def get_env_path(conda_info: dict, env_name: str) -> str:
    """
    Get the full path to a named environment.
    
    Parameters
    ----------
    conda_info : dict
        Output from get_conda_info()
    env_name : str
        Name of the environment
        
    Returns
    -------
    str
        Full path to the environment, or None if not found
    """
    # Check in listed environments
    envs = conda_info.get("envs", [])
    for env_path in envs:
        if Path(env_path).name == env_name:
            return env_path
    
    # Not found in current envs, return expected path in first envs_dir
    envs_dirs = get_envs_dirs(conda_info)
    if envs_dirs:
        return str(Path(envs_dirs[0]) / env_name)
    
    return None


def env_exists(conda_info: dict, env_name: str) -> bool:
    """Check if a conda environment exists."""
    envs = conda_info.get("envs", [])
    for env_path in envs:
        if Path(env_path).name == env_name:
            return True
    return False


def refresh_conda_info() -> dict:
    """Get fresh conda info (call after creating/removing envs)."""
    return get_conda_info()


def create_environment(conda_exe: str, env_name: str, config: dict) -> bool:
    """Create a single conda environment."""
    python_version = config["python_version"]
    packages = config["packages"]
    description = config["description"]
    
    print_step(f"Creating {env_name}: {description}")
    
    # Check if already exists
    conda_info = refresh_conda_info()
    if env_exists(conda_info, env_name):
        print(f"  Environment '{env_name}' already exists. Skipping.")
        return True
    
    # Create environment
    cmd = [
        conda_exe, "create", "-n", env_name,
        f"python={python_version}",
        "-y", "-q"
    ]
    
    try:
        run_command(cmd)
    except RuntimeError as e:
        print(f"  Failed to create environment: {e}")
        return False
    
    # Install packages
    if packages:
        print(f"  Installing packages: {', '.join(packages)}")
        cmd = [conda_exe, "run", "-n", env_name, "pip", "install"] + packages + ["-q"]
        try:
            run_command(cmd)
        except RuntimeError as e:
            print(f"  Warning: Failed to install some packages: {e}")
    
    print(f"  + Created {env_name}")
    return True


def remove_environment(conda_exe: str, env_name: str) -> bool:
    """Remove a conda environment."""
    print_step(f"Removing {env_name}")
    
    # Check if exists
    conda_info = refresh_conda_info()
    if not env_exists(conda_info, env_name):
        print(f"  Environment '{env_name}' does not exist. Skipping.")
        return True
    
    cmd = [conda_exe, "env", "remove", "-n", env_name, "-y"]
    
    try:
        run_command(cmd)
        print(f"  + Removed {env_name}")
        return True
    except RuntimeError as e:
        print(f"  Failed to remove environment: {e}")
        return False


def get_env_python_path(conda_exe: str, env_name: str) -> str:
    """Get the Python executable path for an environment."""
    result = subprocess.run(
        [conda_exe, "run", "-n", env_name, "python", "-c", "import sys; print(sys.executable)"],
        capture_output=True,
        text=True
    )
    
    if result.returncode == 0:
        return result.stdout.strip()
    return "Unknown"


def setup_all(conda_info: dict, conda_exe: str):
    """Create all test environments."""
    print_header("Setting Up Test Environments")
    
    envs_dirs = get_envs_dirs(conda_info)
    
    print(f"Conda executable: {conda_exe}")
    print(f"Conda root: {conda_info.get('root_prefix', 'Unknown')}")
    print(f"Environments directory: {envs_dirs[0] if envs_dirs else 'Unknown'}")
    print(f"Platform: {conda_info.get('platform', 'Unknown')}")
    
    success_count = 0
    
    for env_name, config in ENVIRONMENTS.items():
        if create_environment(conda_exe, env_name, config):
            success_count += 1
    
    print_header("Setup Summary")
    
    # Refresh to get updated env list
    conda_info = refresh_conda_info()
    
    print("\n  Environment Status:")
    print("  " + "-" * 50)
    
    for env_name, config in ENVIRONMENTS.items():
        exists = env_exists(conda_info, env_name)
        status_str = "+ Ready" if exists else "x Missing"
        
        if exists:
            python_path = get_env_python_path(conda_exe, env_name)
            print(f"  {env_name}: {status_str}")
            print(f"         Python: {python_path}")
        else:
            print(f"  {env_name}: {status_str}")
    
    print("  " + "-" * 50)
    print(f"\n  Created: {success_count}/{len(ENVIRONMENTS)} environments")
    
    if success_count == len(ENVIRONMENTS):
        print("\n+ All environments ready! You can now run the tests.")
        print("\nNext steps:")
        print("  1. Activate any environment: conda activate env_a")
        print("  2. Run tests: python run_tests.py all")
        print("\nOr run from all environments:")
        print("  python run_full_test_suite_universal.py")
        return 0
    else:
        print("\nx Some environments failed to create.")
        return 1


def clean_all(conda_info: dict, conda_exe: str):
    """Remove all test environments."""
    print_header("Cleaning Up Test Environments")
    
    envs_dirs = get_envs_dirs(conda_info)
    
    print(f"Conda executable: {conda_exe}")
    print(f"Environments directory: {envs_dirs[0] if envs_dirs else 'Unknown'}")
    
    success_count = 0
    
    for env_name in ENVIRONMENTS:
        if remove_environment(conda_exe, env_name):
            success_count += 1
    
    print_header("Cleanup Summary")
    print(f"\n  Removed: {success_count}/{len(ENVIRONMENTS)} environments")
    
    if success_count == len(ENVIRONMENTS):
        print("\n+ All test environments removed.")
        return 0
    else:
        print("\nx Some environments failed to remove.")
        return 1


def check_all(conda_info: dict, conda_exe: str):
    """Check status of all test environments."""
    print_header("Test Environment Status")
    
    envs_dirs = get_envs_dirs(conda_info)
    
    print(f"Conda executable: {conda_exe}")
    print(f"Conda root: {conda_info.get('root_prefix', 'Unknown')}")
    print(f"Environments directory: {envs_dirs[0] if envs_dirs else 'Unknown'}")
    
    print("\n  Environment Status:")
    print("  " + "-" * 60)
    
    all_ready = True
    
    for env_name, config in ENVIRONMENTS.items():
        exists = env_exists(conda_info, env_name)
        status_str = "+ Ready" if exists else "x Missing"
        
        if not exists:
            all_ready = False
        
        print(f"  {env_name} ({config['description']})")
        print(f"      Status: {status_str}")
        
        if exists:
            env_path = get_env_path(conda_info, env_name)
            python_path = get_env_python_path(conda_exe, env_name)
            print(f"      Path: {env_path}")
            print(f"      Python: {python_path}")
        
        print()
    
    print("  " + "-" * 60)
    
    if all_ready:
        print("\n+ All environments are ready.")
        return 0
    else:
        print("\nx Some environments are missing. Run: python setup_environments.py")
        return 1


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Setup Conda environments for pipeline testing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python setup_environments.py          # Create all environments
  python setup_environments.py --clean  # Remove all environments
  python setup_environments.py --check  # Check environment status
        """
    )
    
    parser.add_argument(
        "--clean", 
        action="store_true",
        help="Remove all test environments"
    )
    
    parser.add_argument(
        "--check",
        action="store_true", 
        help="Check if environments exist"
    )
    
    args = parser.parse_args()
    
    # Get conda info (single source of truth)
    try:
        conda_info = get_conda_info()
        conda_exe = get_conda_executable(conda_info)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        return 1
    
    # Execute requested action
    if args.clean:
        return clean_all(conda_info, conda_exe)
    elif args.check:
        return check_all(conda_info, conda_exe)
    else:
        return setup_all(conda_info, conda_exe)


if __name__ == "__main__":
    sys.exit(main())
