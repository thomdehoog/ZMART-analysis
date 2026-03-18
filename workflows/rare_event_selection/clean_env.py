"""
Remove conda environments for the rare_event_selection workflow.

Finds and removes all environments matching the SMART--rare_event_selection--*
naming pattern.

Usage:
    python clean_env.py             # remove all envs for this workflow
    python clean_env.py --step main # remove only the main env
    python clean_env.py --dry-run   # list without removing

Requirements:
    - conda (Miniconda or Anaconda)
    - Run from a conda-enabled terminal
"""

import subprocess
import sys
import platform
import argparse
import json
from pathlib import Path


WORKFLOW = "rare_event_selection"
PREFIX = f"SMART--{WORKFLOW}--"


def get_conda_info():
    """Get conda configuration via 'conda info --json'.

    Returns
    -------
    dict
        Parsed JSON output from conda info.

    Raises
    ------
    FileNotFoundError
        If conda is not available.
    """
    try:
        result = subprocess.run(
            ["conda", "info", "--json"],
            capture_output=True, text=True,
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


def get_conda_exe(conda_info):
    """Get the conda executable path from conda info."""
    conda_exe = conda_info.get("conda_exe")
    if conda_exe and Path(conda_exe).exists():
        return conda_exe

    root_prefix = conda_info.get("root_prefix", "")
    if root_prefix:
        if platform.system() == "Windows":
            candidate = Path(root_prefix) / "Scripts" / "conda.exe"
        else:
            candidate = Path(root_prefix) / "bin" / "conda"
        if candidate.exists():
            return str(candidate)

    return "conda"


def list_workflow_envs(conda_info):
    """List all conda envs matching the workflow prefix."""
    matching = []
    for env_path in conda_info.get("envs", []):
        name = Path(env_path).name
        if name.startswith(PREFIX):
            matching.append(name)
    return matching


def main():
    parser = argparse.ArgumentParser(
        description=f"Remove conda envs for {WORKFLOW} workflow"
    )
    parser.add_argument(
        "--step", default=None,
        help="Remove only this step's env (e.g. main, segment). "
             "If omitted, removes all envs for this workflow.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List envs without removing",
    )
    args = parser.parse_args()

    try:
        conda_info = get_conda_info()
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    conda = get_conda_exe(conda_info)

    if args.step:
        targets = [f"{PREFIX}{args.step}"]
    else:
        targets = list_workflow_envs(conda_info)

    if not targets:
        print(f"No environments found matching {PREFIX}*")
        return

    print(f"Environments to remove ({len(targets)}):")
    for name in targets:
        print(f"  {name}")

    if args.dry_run:
        print("\n(dry run — nothing removed)")
        return

    for name in targets:
        print(f"\nRemoving {name}...")
        subprocess.run(
            [conda, "env", "remove", "-n", name, "-y"],
            check=True,
        )

    print(f"\nDone. Removed {len(targets)} environment(s).")


if __name__ == "__main__":
    main()
