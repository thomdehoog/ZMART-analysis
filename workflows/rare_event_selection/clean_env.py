"""
Remove conda environments for the rare_event_selection workflow.

Finds and removes all environments matching the SMART--rare_event_selection--*
naming pattern.

Usage:
    python clean_env.py             # remove all envs for this workflow
    python clean_env.py --step main # remove only the main env
    python clean_env.py --dry-run   # list without removing
"""

import subprocess
import sys
import platform
import argparse
import shutil
import json


WORKFLOW = "rare_event_selection"
PREFIX = f"SMART--{WORKFLOW}--"


def find_conda():
    """Find the conda executable."""
    conda = shutil.which("conda")
    if conda:
        return conda

    import os

    candidates = []
    if platform.system() == "Windows":
        candidates = [
            r"C:\ProgramData\Miniconda3\condabin\conda.bat",
            r"C:\ProgramData\MinicondaZMB\condabin\conda.bat",
            r"C:\Users\{}\Miniconda3\condabin\conda.bat".format(os.getlogin()),
        ]
    elif platform.system() == "Darwin":
        candidates = [
            "/opt/homebrew/Caskroom/miniconda/base/condabin/conda",
            "/usr/local/Caskroom/miniconda/base/condabin/conda",
            "~/miniconda3/condabin/conda",
        ]
    else:
        candidates = [
            "/opt/conda/condabin/conda",
            "~/miniconda3/condabin/conda",
            "~/anaconda3/condabin/conda",
        ]

    for path in candidates:
        path = os.path.expanduser(path)
        if os.path.exists(path):
            return path

    return None


def list_workflow_envs(conda):
    """List all conda envs matching the workflow prefix."""
    result = subprocess.run(
        [conda, "env", "list", "--json"],
        capture_output=True, text=True,
    )
    envs = json.loads(result.stdout).get("envs", [])

    import os

    matching = []
    for env_path in envs:
        name = os.path.basename(env_path)
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

    conda = find_conda()
    if not conda:
        print("ERROR: conda not found.")
        sys.exit(1)

    if args.step:
        targets = [f"{PREFIX}{args.step}"]
    else:
        targets = list_workflow_envs(conda)

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
