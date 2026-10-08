"""Shared pytest set-up for the workflow tests.

A step file is loaded on its own, not as part of a package, and the tests
call the steps directly. So this puts on Python's search path what a step
expects to find there: the ``workflows`` folder, for ``shared`` and each
workflow's ``parts``, and every workflow's ``steps`` folder, so a test can
simply ``import detect_objects``.
"""

from __future__ import annotations

import sys
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent

for folder in [WORKFLOWS, *sorted(WORKFLOWS.glob("*/steps"))]:
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
