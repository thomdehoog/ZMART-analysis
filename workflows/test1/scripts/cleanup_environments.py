#!/usr/bin/env python
"""
Cleanup Test Environments

This script removes the test Conda environments (env_a, env_b, env_c)
that were created by setup_environments.py.

Directory Structure:
    analysis_workflows/
    ├── engine/
    │   └── engine.py
    └── workflows/
        └── test1/
            ├── scripts/
            │   ├── cleanup_environments.py   # THIS FILE
            │   └── ...
            ├── steps/
            └── pipelines/

Usage:
    python cleanup_environments.py          # Remove all test environments
    python cleanup_environments.py --check  # Check status without removing

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


# Environments to remove
ENVIRONMENTS = ["env_a", "env_b", "env_c"]


def print_header(title: str):
    """Print a formatted header."""
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60)


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


def get_env_size(env_path: str) -> str:
    """Estimate environment size."""
    if not env_path or not os.path.isdir(env_path):
        return "unknown size"
    
    try:
        total = 0
        for dirpath, dirnames, filenames in os.walk(env_path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if os.path.exists(fp):
                    total += os.path.getsize(fp)
        
        # Format size
        if total > 1024 * 1024 * 1024:
            return f"{total / (1024**3):.1f} GB"
        elif total > 1024 * 1024:
            return f"{total / (1024**2):.0f} MB"
        else:
            return f"{total / 1024:.0f} KB"
    except:
        return "unknown size"


def remove_environment(conda_exe: str, env_name: str) -> bool:
    """Remove a conda environment."""
    print(f"\n  Removing {env_name}...", end=" ", flush=True)
    
    # Check if exists
    conda_info = refresh_conda_info()
    if not env_exists(conda_info, env_name):
        print("(not found, skipping)")
        return True
    
    cmd = [conda_exe, "env", "remove", "-n", env_name, "-y"]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode == 0:
        print("+ removed")
        return True
    else:
        print("x failed")
        if result.stderr:
            print(f"    Error: {result.stderr.strip()}")
        return False


def check_environments(conda_info: dict):
    """Check and display status of test environments."""
    print_header("Test Environment Status")
    
    envs_dirs = get_envs_dirs(conda_info)
    
    print(f"\n  Environments directory: {envs_dirs[0] if envs_dirs else 'Unknown'}")
    print("  " + "-" * 45)
    
    found_count = 0
    
    for env_name in ENVIRONMENTS:
        exists = env_exists(conda_info, env_name)
        
        if exists:
            found_count += 1
            env_path = get_env_path(conda_info, env_name)
            size = get_env_size(env_path)
            print(f"  + {env_name}: Found ({size})")
            print(f"      Path: {env_path}")
        else:
            print(f"  x {env_name}: Not found")
    
    print("  " + "-" * 45)
    
    if found_count == 0:
        print("\n  No test environments found. Nothing to clean up.")
    elif found_count == len(ENVIRONMENTS):
        print(f"\n  All {found_count} test environments exist.")
        print("  Run without --check to remove them.")
    else:
        print(f"\n  {found_count}/{len(ENVIRONMENTS)} test environments found.")


def cleanup_all(conda_info: dict, conda_exe: str) -> int:
    """Remove all test environments."""
    print_header("Cleaning Up Test Environments")
    
    envs_dirs = get_envs_dirs(conda_info)
    
    print(f"\n  Environments directory: {envs_dirs[0] if envs_dirs else 'Unknown'}")
    print(f"\n  This will remove the following Conda environments:")
    
    for env_name in ENVIRONMENTS:
        exists = env_exists(conda_info, env_name)
        if exists:
            env_path = get_env_path(conda_info, env_name)
            size = get_env_size(env_path)
            print(f"    - {env_name} ({size})")
        else:
            print(f"    - {env_name} (not found)")
    
    # Confirm
    print()
    try:
        response = input("  Proceed with removal? [y/N]: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print("\n\n  Cancelled.")
        return 0
    
    if response not in ('y', 'yes'):
        print("\n  Cancelled. No environments were removed.")
        return 0
    
    # Remove environments
    print("\n  Removing environments...")
    
    success_count = 0
    for env_name in ENVIRONMENTS:
        if remove_environment(conda_exe, env_name):
            success_count += 1
    
    # Summary
    print_header("Cleanup Summary")
    
    print(f"\n  Removed: {success_count}/{len(ENVIRONMENTS)} environments")
    
    if success_count == len(ENVIRONMENTS):
        print("\n  + All test environments have been removed.")
        print("    Disk space has been freed.")
        return 0
    elif success_count > 0:
        print("\n  ! Some environments could not be removed.")
        print("    Try closing any terminals using these environments.")
        return 1
    else:
        print("\n  x No environments were removed.")
        return 1


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Remove test Conda environments",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python cleanup_environments.py          # Remove all test environments
  python cleanup_environments.py --check  # Check status only
        """
    )
    
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check environment status without removing"
    )
    
    parser.add_argument(
        "-y", "--yes",
        action="store_true",
        help="Skip confirmation prompt"
    )
    
    args = parser.parse_args()
    
    # Print banner
    print()
    print("  +--------------------------------------------+")
    print("  |   Pipeline Test Environment Cleanup        |")
    print("  +--------------------------------------------+")
    
    # Get conda info (single source of truth)
    try:
        conda_info = get_conda_info()
        conda_exe = get_conda_executable(conda_info)
        print(f"\n  Conda: {conda_exe}")
        print(f"  Root: {conda_info.get('root_prefix', 'Unknown')}")
    except FileNotFoundError as e:
        print(f"\n  Error: {e}")
        return 1
    
    # Execute requested action
    if args.check:
        check_environments(conda_info)
        return 0
    else:
        if args.yes:
            # Skip confirmation
            print_header("Cleaning Up Test Environments")
            
            envs_dirs = get_envs_dirs(conda_info)
            print(f"\n  Environments directory: {envs_dirs[0] if envs_dirs else 'Unknown'}")
            print("\n  Removing environments (--yes flag provided)...")
            
            success_count = 0
            for env_name in ENVIRONMENTS:
                if remove_environment(conda_exe, env_name):
                    success_count += 1
            
            print_header("Cleanup Summary")
            print(f"\n  Removed: {success_count}/{len(ENVIRONMENTS)} environments")
            
            return 0 if success_count == len(ENVIRONMENTS) else 1
        else:
            return cleanup_all(conda_info, conda_exe)


if __name__ == "__main__":
    sys.exit(main())
