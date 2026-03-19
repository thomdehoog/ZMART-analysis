"""Step loading and METADATA extraction."""

import types
from pathlib import Path
from typing import Any, Dict


def load_function(func_name: str, functions_dir: Path):
    """
    Load a step module from the functions directory.

    Uses exec-based loading instead of importlib.util.spec_from_file_location
    to avoid Windows DLL search path side effects that can break packages
    like PyTorch when step files are on network drives.
    """
    func_path = functions_dir / f"{func_name}.py"

    if not func_path.exists():
        raise FileNotFoundError(f"Function file not found: {func_path}")

    namespace = {"__name__": func_name, "__file__": str(func_path)}
    with open(func_path) as f:
        exec(compile(f.read(), str(func_path), "exec"), namespace)

    module = types.ModuleType(func_name)
    module.__dict__.update(namespace)
    return module


def get_step_settings(module) -> Dict[str, Any]:
    """
    Get execution settings from a step's METADATA.

    Returns
    -------
    dict
        - environment: "local" or conda environment name
        - worker: "persistent" or "subprocess"
        - max_workers: max concurrent workers (int, default 1)
    """
    metadata = getattr(module, "METADATA", {})

    return {
        "environment": metadata.get("environment", "local"),
        "worker": metadata.get("worker", "subprocess"),
        "max_workers": metadata.get("max_workers", 1),
    }
