"""
Pipeline Engine

An orchestration engine for executing sequences of Python functions
defined in YAML configuration files.
"""

from .engine import run_pipeline

__version__ = "1.0.0"
__all__ = ["run_pipeline"]
