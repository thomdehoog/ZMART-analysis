"""
Create conda environment for the rare_event_selection workflow.

Uses conda for the Python interpreter, pip for all packages.
This avoids DLL conflicts between scipy and torch on Windows
that occur when mixing conda and pip packages.

Usage:
    python setup_env.py              # auto-detects GPU (CUDA/MPS/CPU)
    python setup_env.py --step segment
    python setup_env.py --gpu cpu    # force CPU
    python setup_env.py --dry-run

Naming convention:
    SMART--{workflow}--main     -- default env for the workflow
    SMART--{workflow}--{step}   -- isolated env for a specific step

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
from conda_utils import (
    get_conda_info, get_conda_exe, env_exists,
    detect_gpu, gpu_label, get_torch_install_args,
)


WORKFLOW = "rare_event_selection"
PYTHON_VERSION = "3.12"

PIP_PACKAGES = [
    "pyyaml",
    "numpy",
    "scikit-image",
    "pooch",
    "cellpose",
]

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
    print(f"  [ OK ]    {message}")

def fail(message):
    print(f"  [FAIL]    {message}")

def skip(message):
    print(f"  [SKIP]    {message}")

def warn(message):
    print(f"  [WARN]    {message}")

def cmd_line(cmd):
    print(f"  [ RUN]    {' '.join(cmd)}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=f"Set up conda env for {WORKFLOW} workflow"
    )
    parser.add_argument(
        "--step", default="main",
        help="Step name for isolation (default: main)",
    )
    parser.add_argument(
        "--python", default=PYTHON_VERSION,
        help=f"Python version (default: {PYTHON_VERSION})",
    )
    parser.add_argument(
        "--gpu", default=None,
        help="GPU backend: cu128, cu124, cu121, mps, cpu (default: auto-detect)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print commands without executing",
    )
    args = parser.parse_args()

    env_name = f"SMART--{WORKFLOW}--{args.step}"
    gpu = args.gpu or detect_gpu()
    t_start = time.time()

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------
    banner("SMART Analysis -- Environment Setup")

    section("System")
    import platform as pf
    info("Platform", f"{pf.system()} ({pf.machine()})")
    info("Python target", args.python)
    info("GPU backend", gpu_label(gpu))

    # Conda info (needed early for version check)
    try:
        conda_info = get_conda_info()
    except FileNotFoundError as e:
        fail(str(e))
        sys.exit(1)

    conda_version = conda_info.get("conda_version", "unknown")
    conda_warn = ""
    if conda_version != "unknown":
        parts = conda_version.split(".")
        major_minor = float(f"{parts[0]}.{parts[1]}")
        if major_minor < 25.7:
            conda_warn = " (< 25.7 -- env switching may be unstable)"
    info("Conda version", conda_version + conda_warn)

    # ------------------------------------------------------------------
    # Conda
    # ------------------------------------------------------------------
    section("Conda")
    conda = get_conda_exe(conda_info)
    envs_dirs = conda_info.get("envs_dirs", [])

    info("Executable", conda)
    info("Root prefix", conda_info.get("root_prefix", "unknown"))
    info("Envs directory", envs_dirs[0] if envs_dirs else "unknown")

    if env_exists(conda_info, env_name):
        fail(f"Environment '{env_name}' already exists.")
        print(f"         Remove it first:  python clean_env.py --step {args.step}")
        sys.exit(1)

    section("Environment")
    info("Name", env_name)
    info("Workflow", WORKFLOW)
    info("Step", args.step)

    # ------------------------------------------------------------------
    # Step 1: Create conda env
    # ------------------------------------------------------------------
    step(1, 4, "Creating conda environment")
    create_cmd = [conda, "create", "-n", env_name,
                  f"python={args.python}", "-y", "-q"]
    cmd_line(create_cmd)
    if args.dry_run:
        skip("dry run")
    else:
        result = subprocess.run(create_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            fail("Failed to create environment")
            print(result.stderr)
            sys.exit(1)
        ok(f"Created {env_name}")

    # ------------------------------------------------------------------
    # Step 2: Install PyTorch
    # ------------------------------------------------------------------
    torch_args = get_torch_install_args(gpu)

    step(2, 4, f"Installing PyTorch ({gpu_label(gpu)})")
    pip_cmd = [conda, "run", "--no-capture-output", "-n", env_name,
               "pip", "install"] + torch_args
    cmd_line(pip_cmd)
    if args.dry_run:
        skip("dry run")
    else:
        result = subprocess.run(pip_cmd)
        if result.returncode != 0:
            fail("PyTorch installation failed")
            sys.exit(1)
        ok("PyTorch installed")

    # ------------------------------------------------------------------
    # Step 3: Install packages
    # ------------------------------------------------------------------
    step(3, 4, "Installing analysis packages")
    pip_cmd = [conda, "run", "--no-capture-output", "-n", env_name,
               "pip", "install"] + PIP_PACKAGES
    print(f"  Packages: {', '.join(PIP_PACKAGES)}")
    cmd_line(pip_cmd)
    if args.dry_run:
        skip("dry run")
    else:
        result = subprocess.run(pip_cmd)
        if result.returncode != 0:
            fail("Package installation failed")
            sys.exit(1)
        for pkg in PIP_PACKAGES:
            ok(pkg)

    # ------------------------------------------------------------------
    # Step 4: Diagnostics
    # ------------------------------------------------------------------
    step(4, 4, "Running diagnostics")

    if args.dry_run:
        skip("dry run")
    else:
        checks = [
            ("PyTorch loads",
             "import torch; print(torch.__version__)"),
            ("GPU backend available",
             "import torch; "
             "cuda = torch.cuda.is_available(); "
             "mps = hasattr(torch.backends, 'mps') and torch.backends.mps.is_available(); "
             "b = 'CUDA' if cuda else ('MPS' if mps else 'CPU'); "
             "print(b)"),
            ("scikit-image",
             "from skimage.filters import gaussian; "
             "import numpy as np; "
             "gaussian(np.zeros((10,10)), sigma=1); "
             "print('OK')"),
            ("scipy + torch coexist",
             "from skimage.filters import gaussian; "
             "import numpy as np; "
             "gaussian(np.zeros((10,10)), sigma=1); "
             "import torch; print('OK')"),
            ("cellpose",
             "from cellpose import models; print('OK')"),
        ]

        all_passed = True
        for label, code in checks:
            result = subprocess.run(
                [conda, "run", "--no-capture-output", "-n", env_name,
                 "python", "-c", code],
                capture_output=True, text=True,
            )
            output = result.stdout.strip()
            if result.returncode == 0:
                ok(f"{label:<28s} {output}")
            else:
                fail(f"{label:<28s}")
                all_passed = False

        if not all_passed:
            banner("Setup FAILED")
            print("  Some diagnostics did not pass.")
            print("  Review the output above and check your installation.")
            sys.exit(1)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    elapsed = time.time() - t_start

    banner("Setup Complete")

    section("Summary")
    info("Environment", env_name)
    info("GPU backend", gpu_label(gpu))
    info("Python", args.python)
    info("Packages", str(len(PIP_PACKAGES) + 2))
    info("Time", f"{elapsed:.0f}s")

    section("Next steps")
    print(f"  Activate:    conda activate {env_name}")
    print(f"  Remove:      python clean_env.py --step {args.step}")
    print()
    print("=" * WIDTH)


if __name__ == "__main__":
    main()
