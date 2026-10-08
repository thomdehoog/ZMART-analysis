"""
Finding conda, and asking it which environments exist.

The engine needs this at run time: a worker is started with ``conda run``
inside the environment a step names, so the engine has to know where conda
is and where that environment lives. The environment setup and cleanup
scripts build on the same few functions (see ``environments.py``).

Only the standard library is used here, so this works in any Python that
can reach conda.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _find_conda() -> str:
    """The conda program to run.

    Conda sets ``CONDA_EXE`` when an environment is activated, and every
    program started from there inherits it. A shell where conda was never
    activated (Git Bash, a scheduled task) has neither that nor conda on its
    PATH, so as a last resort conda is looked for in the installation this
    Python itself belongs to: either this is conda's own Python, or one in
    its ``envs`` folder.
    """
    exe = os.environ.get("CONDA_EXE")
    if exe:
        return exe
    found = shutil.which("conda")
    if found:
        return found
    here = Path(sys.prefix)
    for root in (here, *here.parents[1:2]):
        for candidate in (root / "Scripts" / "conda.exe", root / "bin" / "conda"):
            if candidate.is_file():
                return str(candidate)
    return "conda"


CONDA_CMD = _find_conda()


def get_conda_info():
    """Conda's configuration, from ``conda info --json``: the executable,
    the environment directories, and the environments that exist.

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
            [CONDA_CMD, "info", "--json"],
            capture_output=True,
            text=True,
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
    return CONDA_CMD


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
