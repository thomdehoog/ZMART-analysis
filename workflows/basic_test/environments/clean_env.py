"""
Remove conda environments for the basic_test workflow.

Finds and removes all environments matching the SMART--basic_test--*
naming pattern.

Usage:
    python clean_env.py               # remove all envs for this workflow
    python clean_env.py --step env_a  # remove only env_a
    python clean_env.py --dry-run     # list without removing

Requirements:
    - conda (Miniconda or Anaconda)
    - Run from a conda-enabled terminal
"""

import subprocess
import sys
import argparse
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "engine"))
from conda_utils import get_conda_info, get_conda_exe, list_envs_by_prefix


WORKFLOW = "basic_test"
PREFIX = f"SMART--{WORKFLOW}--"

WIDTH = 70


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def banner(title):
    print()
    print("=" * WIDTH)
    print(f"  {title}")
    print("=" * WIDTH)

def section(title):
    print()
    print(f"  {title}")
    print(f"  {'-' * (WIDTH - 4)}")

def info(label, value):
    print(f"  {label + ':':<24s} {value}")

def ok(message):
    print(f"  [OK]   {message}")

def fail(message):
    print(f"  [FAIL] {message}")

def skip(message):
    print(f"  [SKIP] {message}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=f"Remove conda envs for {WORKFLOW} workflow"
    )
    parser.add_argument(
        "--step", default=None,
        help="Remove only this step's env (e.g. env_a). "
             "If omitted, removes all envs for this workflow.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List envs without removing",
    )
    args = parser.parse_args()

    t_start = time.time()

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------
    banner("SMART Analysis -- Environment Cleanup")

    section("Conda")
    try:
        conda_info = get_conda_info()
    except FileNotFoundError as e:
        fail(str(e))
        sys.exit(1)

    conda = get_conda_exe(conda_info)
    envs_dirs = conda_info.get("envs_dirs", [])

    info("Executable", conda)
    info("Envs directory", envs_dirs[0] if envs_dirs else "unknown")

    # ------------------------------------------------------------------
    # Find targets
    # ------------------------------------------------------------------
    if args.step:
        target_name = f"{PREFIX}{args.step}"
        all_matching = list_envs_by_prefix(conda_info, PREFIX)
        targets = [target_name] if target_name in all_matching else []
        if not targets:
            section("Result")
            fail(f"Environment '{target_name}' not found")
            if all_matching:
                print(f"  Available environments:")
                for name in all_matching:
                    print(f"    {name}")
            else:
                print(f"  No environments matching {PREFIX}*")
            print()
            print("=" * WIDTH)
            sys.exit(1)
    else:
        targets = list_envs_by_prefix(conda_info, PREFIX)

    section("Environments found")
    if not targets:
        info("Matching", f"0 (pattern: {PREFIX}*)")
        banner("Nothing to remove")
        return

    info("Matching", str(len(targets)))
    for name in targets:
        print(f"    {name}")

    # ------------------------------------------------------------------
    # Remove
    # ------------------------------------------------------------------
    if args.dry_run:
        section("Result")
        skip(f"dry run -- {len(targets)} environment(s) would be removed")
        print()
        print("=" * WIDTH)
        return

    section("Removing")
    removed = 0
    for name in targets:
        cmd = [conda, "env", "remove", "-n", name, "-y"]
        print(f"  [RUN]  {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            ok(name)
            removed += 1
        else:
            fail(name)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    elapsed = time.time() - t_start

    banner("Cleanup Complete")

    section("Summary")
    info("Removed", f"{removed}/{len(targets)} environment(s)")
    info("Time", f"{elapsed:.0f}s")

    if removed < len(targets):
        print()
        fail("Some environments could not be removed")

    print()
    print("=" * WIDTH)


if __name__ == "__main__":
    main()
