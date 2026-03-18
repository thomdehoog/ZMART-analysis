"""
Create conda environments for the basic_test workflow.

Creates three lightweight environments to test the engine's
environment switching functionality:
    SMART--basic_test--env_a   Python 3.10
    SMART--basic_test--env_b   Python 3.11
    SMART--basic_test--env_c   Python 3.12

Usage:
    python setup_env.py              # create all three envs
    python setup_env.py --step env_a # create only env_a
    python setup_env.py --dry-run

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
from conda_utils import get_conda_info, get_conda_exe, env_exists


WORKFLOW = "basic_test"

ENVIRONMENTS = {
    "env_a": {"python": "3.10", "description": "Test environment A"},
    "env_b": {"python": "3.11", "description": "Test environment B"},
    "env_c": {"python": "3.12", "description": "Test environment C"},
}

PIP_PACKAGES = ["pyyaml"]

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

def step(number, total, description):
    print()
    print(f"  [{number}/{total}] {description}")
    print(f"  {'-' * (WIDTH - 4)}")

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
        description=f"Set up conda envs for {WORKFLOW} workflow"
    )
    parser.add_argument(
        "--step", default=None,
        help="Create only this env (e.g. env_a). If omitted, creates all.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print commands without executing",
    )
    args = parser.parse_args()

    t_start = time.time()

    # Determine which envs to create
    if args.step:
        if args.step not in ENVIRONMENTS:
            print(f"ERROR: Unknown step '{args.step}'. "
                  f"Choose from: {', '.join(ENVIRONMENTS)}")
            sys.exit(1)
        targets = {args.step: ENVIRONMENTS[args.step]}
    else:
        targets = ENVIRONMENTS

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------
    banner("SMART Analysis -- Environment Setup")

    section("System")
    import platform as pf
    info("Platform", f"{pf.system()} ({pf.machine()})")
    info("Workflow", WORKFLOW)
    info("Environments", str(len(targets)))

    # ------------------------------------------------------------------
    # Conda
    # ------------------------------------------------------------------
    section("Conda")
    try:
        conda_info = get_conda_info()
    except FileNotFoundError as e:
        fail(str(e))
        sys.exit(1)

    conda = get_conda_exe(conda_info)
    envs_dirs = conda_info.get("envs_dirs", [])

    info("Executable", conda)
    info("Root prefix", conda_info.get("root_prefix", "unknown"))
    info("Envs directory", envs_dirs[0] if envs_dirs else "unknown")
    conda_version = conda_info.get("conda_version", "unknown")
    info("Conda version", conda_version)

    if conda_version != "unknown":
        parts = conda_version.split(".")
        major_minor = float(f"{parts[0]}.{parts[1]}")
        if major_minor < 25.7:
            print()
            print("  [WARN] Conda version < 25.7 detected.")
            print("         Environment switching may be unstable.")
            print("         Consider: conda update -n base conda")

    # ------------------------------------------------------------------
    # Planned environments
    # ------------------------------------------------------------------
    section("Environments to create")
    for step_name, config in targets.items():
        env_name = f"SMART--{WORKFLOW}--{step_name}"
        exists = env_exists(conda_info, env_name)
        status = " (already exists)" if exists else ""
        info(env_name, f"Python {config['python']}{status}")

    # ------------------------------------------------------------------
    # Create environments
    # ------------------------------------------------------------------
    total = len(targets)
    created = 0

    for i, (step_name, config) in enumerate(targets.items(), 1):
        env_name = f"SMART--{WORKFLOW}--{step_name}"

        step(i, total, f"{config['description']} -- {env_name}")

        if env_exists(conda_info, env_name):
            skip(f"Already exists")
            created += 1
            continue

        # Create env
        create_cmd = [conda, "create", "-n", env_name,
                      f"python={config['python']}", "-y", "-q"]
        print(f"  [RUN]  {' '.join(create_cmd)}")
        if args.dry_run:
            skip("dry run")
        else:
            result = subprocess.run(create_cmd, capture_output=True, text=True)
            if result.returncode != 0:
                fail(f"Failed to create {env_name}")
                print(result.stderr)
                continue
            ok(f"Created {env_name}")

        # Install packages
        if PIP_PACKAGES:
            pip_cmd = [conda, "run", "--no-capture-output", "-n", env_name,
                       "pip", "install"] + PIP_PACKAGES + ["-q"]
            print(f"  [RUN]  {' '.join(pip_cmd)}")
            if args.dry_run:
                skip("dry run")
            else:
                result = subprocess.run(pip_cmd, capture_output=True, text=True)
                if result.returncode != 0:
                    fail("Package installation failed")
                    continue
                for pkg in PIP_PACKAGES:
                    ok(pkg)

        # Verify
        if not args.dry_run:
            result = subprocess.run(
                [conda, "run", "--no-capture-output", "-n", env_name,
                 "python", "-c",
                 "import sys, os; "
                 f"print(f'Python {{sys.version.split()[0]}}, "
                 f"env: {{os.path.basename(sys.prefix)}}')"],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                ok(f"Verified: {result.stdout.strip()}")
            else:
                fail("Verification failed")
                continue

        created += 1

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    elapsed = time.time() - t_start

    banner("Setup Complete")

    section("Summary")
    info("Created", f"{created}/{total} environment(s)")
    info("Time", f"{elapsed:.0f}s")

    section("Environments")
    conda_info = get_conda_info()
    for step_name in targets:
        env_name = f"SMART--{WORKFLOW}--{step_name}"
        exists = env_exists(conda_info, env_name)
        status = "[OK]  " if exists else "[FAIL]"
        print(f"  {status} {env_name}")

    section("Next steps")
    print(f"  Remove:      python clean_env.py")
    print()
    print("=" * WIDTH)


if __name__ == "__main__":
    main()
