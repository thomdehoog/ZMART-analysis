"""Remove the conda environments of the driver_configuration workflow.

Removes every ``ZMART--driver_configuration--*`` environment, or one of them with
``--step <name>``. ``--dry-run`` lists them without removing anything.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The repository root goes on the path first, so this runs as a plain script
# from anywhere, whether or not the package has been installed.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from zmart_analysis.environments import clean_workflow_envs  # noqa: E402

if __name__ == "__main__":
    clean_workflow_envs(workflow="driver_configuration")
