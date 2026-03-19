"""Step loading and METADATA extraction."""

import ast
import types
from pathlib import Path


def get_step_settings(step_path: Path) -> dict:
    """
    Extract execution settings from a step file without running it.

    Parses the file's AST to read the METADATA dict literal,
    avoiding execution of module-level code (imports, side effects).

    Returns
    -------
    dict
        - environment: "local" or conda environment name
        - worker: "persistent" or "subprocess"
        - max_workers: max concurrent workers (int, default 1)
    """
    metadata = _extract_metadata(step_path) or {}

    return {
        "environment": metadata.get("environment", "local"),
        "worker": metadata.get("worker", "subprocess"),
        "max_workers": metadata.get("max_workers", 1),
    }


def _extract_metadata(step_path: Path) -> dict:
    """Extract the METADATA dict literal from a step file via AST."""
    with open(step_path) as f:
        tree = ast.parse(f.read())

    for node in ast.iter_child_nodes(tree):
        if (isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "METADATA"):
            return ast.literal_eval(node.value)

    return {}


def load_function(func_name: str, functions_dir: Path):
    """
    Load a step module for in-process execution.

    Uses exec-based loading instead of importlib to avoid
    Windows DLL search path side effects with PyTorch.
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
