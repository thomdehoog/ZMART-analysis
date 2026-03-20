"""
Step loading and METADATA extraction.

Two independent operations for the pipeline engine:

1. get_step_settings(step_path) — Reads a step file's METADATA dict via AST
   parsing. No code is executed. Used by the engine to decide WHERE to run
   a step (which environment, which device).

2. load_function(func_name, functions_dir) — Loads a step module via exec()
   for in-process execution. Only called for steps that run locally
   (needs_isolation == False). Uses exec instead of importlib to avoid
   Windows DLL search path issues with PyTorch on network drives.

Architecture note
-----------------
These two operations are deliberately separate. Routing (get_step_settings)
must never execute module code, because the step may require packages only
available in a remote conda environment. Execution (load_function) only
happens after routing confirms the step runs in the current process.
"""

import ast
import logging
import types
from pathlib import Path

logger = logging.getLogger(__name__)


def get_step_settings(step_path: Path) -> dict:
    """
    Extract execution settings from a step file without running it.

    Parses the file's AST to read the METADATA dict literal,
    avoiding execution of module-level code (imports, side effects).

    Returns
    -------
    dict
        - environment: "local" or conda environment name
        - device: "gpu" or "cpu"
    """
    metadata = _extract_metadata(step_path) or {}
    settings = {
        "environment": metadata.get("environment", "local"),
        "device": metadata.get("device", "cpu"),
    }
    logger.debug("Step settings for %s: environment=%s, device=%s",
                 step_path.name, settings["environment"], settings["device"])
    return settings


def _extract_metadata(step_path: Path) -> dict:
    """Extract the METADATA dict literal from a step file via AST."""
    with open(step_path) as f:
        tree = ast.parse(f.read())

    for node in ast.iter_child_nodes(tree):
        if (isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "METADATA"):
            result = ast.literal_eval(node.value)
            logger.debug("Extracted METADATA from %s (line %d): %s",
                         step_path.name, node.lineno, result)
            return result

    logger.debug("No METADATA found in %s, using defaults", step_path.name)
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

    logger.debug("Loading module '%s' via exec: %s", func_name, func_path)

    namespace = {"__name__": func_name, "__file__": str(func_path)}
    with open(func_path) as f:
        exec(compile(f.read(), str(func_path), "exec"), namespace)

    module = types.ModuleType(func_name)
    module.__dict__.update(namespace)

    logger.debug("Module '%s' loaded (has run=%s, has METADATA=%s)",
                 func_name, hasattr(module, "run"), hasattr(module, "METADATA"))
    return module
