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
    SMART--{workflow}--main     — default env for the workflow
    SMART--{workflow}--{step}   — isolated env for a specific step

Requirements:
    - conda (Miniconda or Anaconda)
    - Run from a conda-enabled terminal
"""

import subprocess
import sys
import platform
import argparse
import shutil
import json
import re
from pathlib import Path


WORKFLOW = "rare_event_selection"
PYTHON_VERSION = "3.12"

PIP_PACKAGES = [
    "pyyaml",
    "numpy",
    "scikit-image",
    "pooch",
    "cellpose",
]


def get_conda_info():
    """Get conda configuration via 'conda info --json'.

    This is the single source of truth for conda executable,
    environment directories, and existing environments.

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


def env_exists(conda_info, env_name):
    """Check if a conda environment exists."""
    for env_path in conda_info.get("envs", []):
        if Path(env_path).name == env_name:
            return True
    return False


def detect_gpu():
    """Auto-detect available GPU acceleration and CUDA version.

    Returns
    -------
    str
        "cu124", "cu121", etc. for NVIDIA CUDA (matched to installed version),
        "mps" for Apple Silicon, "cpu" otherwise.
    """
    system = platform.system()

    # macOS: check for Apple Silicon (MPS)
    if system == "Darwin":
        if platform.machine() == "arm64":
            return "mps"
        return "cpu"

    # Windows/Linux: check for NVIDIA GPU and detect CUDA version
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return "cpu"

    try:
        result = subprocess.run(
            [nvidia_smi], capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return "cpu"

        # nvidia-smi output contains "CUDA Version: XX.Y"
        for line in result.stdout.split("\n"):
            if "CUDA Version" in line:
                match = re.search(r"CUDA Version:\s*(\d+)\.(\d+)", line)
                if match:
                    major, minor = int(match.group(1)), int(match.group(2))
                    cuda_tag = f"cu{major}{minor}"
                    available = ["cu128", "cu126", "cu124", "cu121", "cu118"]
                    if cuda_tag in available:
                        return cuda_tag
                    # Pick the highest compatible version
                    for tag in available:
                        tag_major = int(tag[2:-1])
                        tag_minor = int(tag[-1])
                        if tag_major < major or (tag_major == major and tag_minor <= minor):
                            return tag
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # NVIDIA present but couldn't detect version
    return "cu124"


def get_torch_install_args(gpu):
    """Get pip install arguments for PyTorch based on GPU type."""
    if gpu == "mps":
        return ["torch", "torchvision"]
    elif gpu == "cpu":
        return ["torch", "torchvision",
                "--index-url", "https://download.pytorch.org/whl/cpu"]
    else:
        return ["torch", "torchvision",
                "--index-url", f"https://download.pytorch.org/whl/{gpu}"]


def run(cmd, dry_run=False):
    """Run a command, printing it first."""
    print(f"\n  $ {' '.join(cmd)}")
    if dry_run:
        print("  (dry run — skipped)")
        return
    subprocess.run(cmd, check=True)


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
        help="GPU backend: cu124, cu121, mps, cpu (default: auto-detect)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print commands without executing",
    )
    args = parser.parse_args()

    env_name = f"SMART--{WORKFLOW}--{args.step}"

    system = platform.system()
    print(f"Platform: {system} ({platform.machine()})")

    # Get conda info
    try:
        conda_info = get_conda_info()
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    conda = get_conda_exe(conda_info)
    print(f"Conda: {conda}")

    if env_exists(conda_info, env_name):
        print(f"Environment '{env_name}' already exists. Remove it first with clean_env.py.")
        sys.exit(1)

    # Auto-detect GPU if not specified
    gpu = args.gpu or detect_gpu()
    print(f"GPU backend: {gpu}")
    print(f"Environment: {env_name} (Python {args.python})")

    # Step 1: Create conda env with Python only
    print("\n[1/3] Creating conda environment...")
    run(
        [conda, "create", "-n", env_name, f"python={args.python}", "-y"],
        args.dry_run,
    )

    # Step 2: Build pip command via conda run
    pip_cmd = [conda, "run", "--no-capture-output", "-n", env_name, "pip"]

    # Step 3: Install torch via pip with correct backend
    print("\n[2/3] Installing PyTorch...")
    run(pip_cmd + ["install"] + get_torch_install_args(gpu), args.dry_run)

    # Step 4: Install remaining packages via pip
    print("\n[3/3] Installing packages...")
    run(pip_cmd + ["install"] + PIP_PACKAGES, args.dry_run)

    # Verify
    if not args.dry_run:
        print("\n[verify] Checking installation...")
        subprocess.run(
            [
                conda, "run", "--no-capture-output", "-n", env_name, "python", "-c",
                "import torch; "
                "cuda = torch.cuda.is_available(); "
                "mps = hasattr(torch.backends, 'mps') and torch.backends.mps.is_available(); "
                "backend = 'CUDA' if cuda else ('MPS' if mps else 'CPU'); "
                "print(f'torch {torch.__version__}, backend: {backend}'); "
                "from skimage.filters import gaussian; import numpy as np; "
                "gaussian(np.zeros((10,10)), sigma=1); "
                "import torch; print('scipy+torch DLL test: OK'); "
                "from cellpose import models; print('cellpose OK'); "
                "print('All checks passed')",
            ],
            check=True,
        )

    print(f"\nDone. Activate with: conda activate {env_name}")


if __name__ == "__main__":
    main()
