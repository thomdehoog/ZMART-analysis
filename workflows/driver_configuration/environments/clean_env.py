"""Remove the conda environments of the driver_configuration workflow.

Removes every ``ZMART--driver_configuration--*`` environment, or one of them with
``--step <name>``. ``--dry-run`` lists them without removing anything.
"""

from __future__ import annotations

import sys
from pathlib import Path

# conda_utils lives in the shared engine, three directories up — on the path
# before it is imported, so this runs as a plain script from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "engine"))

from conda_utils import clean_workflow_envs  # noqa: E402


if __name__ == "__main__":
    clean_workflow_envs(workflow="driver_configuration")
