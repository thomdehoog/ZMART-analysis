"""
Shared conda utilities for environment setup and cleanup scripts.

Provides conda discovery, environment queries, and GPU detection.
"""

import subprocess
import sys
import platform
import shutil
import json
import re
import os
from pathlib import Path


def _find_conda_executable():
    """Search for the conda executable on disk.

    Checks PATH first, then common install locations per platform.

    Returns
    -------
    str or None
        Path to conda executable, or None if not found.
    """
    conda = shutil.which("conda")
    if conda:
        return conda

    candidates = []
    if platform.system() == "Windows":
        candidates = [
            r"C:\ProgramData\Miniconda3\condabin\conda.bat",
            r"C:\ProgramData\MinicondaZMB\condabin\conda.bat",
            os.path.expanduser(r"~\Miniconda3\condabin\conda.bat"),
            os.path.expanduser(r"~\Anaconda3\condabin\conda.bat"),
        ]
    elif platform.system() == "Darwin":
        candidates = [
            "/opt/homebrew/Caskroom/miniconda/base/condabin/conda",
            "/usr/local/Caskroom/miniconda/base/condabin/conda",
            os.path.expanduser("~/miniconda3/condabin/conda"),
            os.path.expanduser("~/anaconda3/condabin/conda"),
        ]
    else:
        candidates = [
            "/opt/conda/condabin/conda",
            os.path.expanduser("~/miniconda3/condabin/conda"),
            os.path.expanduser("~/anaconda3/condabin/conda"),
        ]

    for path in candidates:
        if os.path.exists(path):
            return path

    return None


def get_conda_info():
    """Get conda configuration via 'conda info --json'.

    This is the single source of truth for conda executable,
    environment directories, and existing environments. Falls back
    to searching common install locations if conda is not on PATH.

    Returns
    -------
    dict
        Parsed JSON output from conda info.

    Raises
    ------
    FileNotFoundError
        If conda is not available.
    """
    for conda_cmd in ["conda", _find_conda_executable()]:
        if conda_cmd is None:
            continue
        try:
            result = subprocess.run(
                [conda_cmd, "info", "--json"],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                info = json.loads(result.stdout)
                if "conda_exe" not in info or not Path(info["conda_exe"]).exists():
                    info["_conda_cmd"] = conda_cmd
                return info
        except (FileNotFoundError, json.JSONDecodeError):
            continue

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

    fallback = conda_info.get("_conda_cmd")
    if fallback:
        return fallback

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


def list_envs_by_prefix(conda_info, prefix):
    """List all conda envs whose name starts with prefix."""
    matching = []
    for env_path in conda_info.get("envs", []):
        name = Path(env_path).name
        if name.startswith(prefix):
            matching.append(name)
    return matching


def detect_gpu():
    """Auto-detect available GPU acceleration and CUDA version.

    Returns
    -------
    str
        "cu128", "cu124", etc. for NVIDIA CUDA (matched to installed version),
        "mps" for Apple Silicon, "cpu" otherwise.
    """
    system = platform.system()

    if system == "Darwin":
        if platform.machine() == "arm64":
            return "mps"
        return "cpu"

    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return "cpu"

    try:
        result = subprocess.run(
            [nvidia_smi], capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return "cpu"

        for line in result.stdout.split("\n"):
            if "CUDA Version" in line:
                match = re.search(r"CUDA Version:\s*(\d+)\.(\d+)", line)
                if match:
                    major, minor = int(match.group(1)), int(match.group(2))
                    cuda_tag = f"cu{major}{minor}"
                    available = ["cu128", "cu126", "cu124", "cu121", "cu118"]
                    if cuda_tag in available:
                        return cuda_tag
                    for tag in available:
                        tag_major = int(tag[2:-1])
                        tag_minor = int(tag[-1])
                        if tag_major < major or (tag_major == major and tag_minor <= minor):
                            return tag
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return "cu124"


def gpu_label(gpu):
    """Human-readable label for the GPU backend."""
    if gpu == "mps":
        return "Apple MPS (Metal Performance Shaders)"
    elif gpu == "cpu":
        return "CPU (no GPU acceleration)"
    else:
        version = f"{gpu[2:-1]}.{gpu[-1]}"
        return f"NVIDIA CUDA {version}"


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
