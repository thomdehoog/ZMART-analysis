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
"""

import subprocess
import sys
import platform
import argparse
import shutil


WORKFLOW = "rare_event_selection"
PYTHON_VERSION = "3.12"

# Packages installed via pip (order matters for dependency resolution)
PIP_PACKAGES = [
    "pyyaml",
    "numpy",
    "scikit-image",
    "pooch",
    "cellpose",
]


def find_conda():
    """Find the conda executable."""
    conda = shutil.which("conda")
    if conda:
        return conda

    # Common locations
    candidates = []
    if platform.system() == "Windows":
        candidates = [
            r"C:\ProgramData\Miniconda3\condabin\conda.bat",
            r"C:\ProgramData\MinicondaZMB\condabin\conda.bat",
            r"C:\Users\{}\Miniconda3\condabin\conda.bat".format(
                __import__("os").getlogin()
            ),
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

    import os

    for path in candidates:
        path = os.path.expanduser(path)
        if os.path.exists(path):
            return path

    return None


def detect_gpu():
    """Auto-detect available GPU acceleration.

    Returns
    -------
    str
        "cu124" for NVIDIA CUDA, "mps" for Apple Silicon, "cpu" otherwise.
    """
    system = platform.system()

    # macOS: check for Apple Silicon (MPS)
    if system == "Darwin":
        if platform.machine() == "arm64":
            return "mps"
        return "cpu"

    # Windows/Linux: check for NVIDIA GPU
    if shutil.which("nvidia-smi"):
        return "cu124"

    # Fallback: no GPU found
    return "cpu"


def get_torch_install_args(gpu):
    """Get pip install arguments for PyTorch based on GPU type.

    Parameters
    ----------
    gpu : str
        "cu124", "cu121", "mps", or "cpu".

    Returns
    -------
    list[str]
        Arguments to append to `pip install`.
    """
    if gpu == "mps":
        # macOS: default PyPI torch includes MPS support
        return ["torch", "torchvision"]
    elif gpu == "cpu":
        return ["torch", "torchvision", "--index-url",
                "https://download.pytorch.org/whl/cpu"]
    else:
        return ["torch", "torchvision", "--index-url",
                f"https://download.pytorch.org/whl/{gpu}"]


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
        help="Step name for isolation (default: main)"
    )
    parser.add_argument(
        "--python", default=PYTHON_VERSION,
        help=f"Python version (default: {PYTHON_VERSION})"
    )
    parser.add_argument(
        "--gpu", default=None,
        help="GPU backend: cu124, cu121, mps, cpu (default: auto-detect)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print commands without executing"
    )
    args = parser.parse_args()

    env_name = f"SMART--{WORKFLOW}--{args.step}"

    system = platform.system()
    print(f"Platform: {system} ({platform.machine()})")

    # Find conda
    conda = find_conda()
    if not conda:
        print("ERROR: conda not found. Install Miniconda first.")
        print("  https://docs.conda.io/en/latest/miniconda.html")
        sys.exit(1)
    print(f"Conda: {conda}")

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
    pip_cmd = [conda, "run", "-n", env_name, "pip"]

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
                conda, "run", "-n", env_name, "python", "-c",
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
