"""Shared pytest set-up for the workflow tests.

A step file is loaded on its own, not as part of a package, and the tests
call the steps directly. So this puts every workflow's ``steps`` folder on
Python's search path, so a test can simply ``import score_focus``. Both
workflows have a ``read_images`` step: tests load it with
``helpers.load_step(workflow, "read_images")``.
"""

from __future__ import annotations

import sys
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent

for folder in sorted(WORKFLOWS.glob("*/steps")):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
