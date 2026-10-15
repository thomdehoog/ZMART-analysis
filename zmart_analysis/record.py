"""
Record -- the run record of one registered pipeline, ``run.jsonl``.

One line of JSON per event: register, submit, a job's end, deregister.
Each line is on disk before the next event, so the record can be read while
the pipeline runs and keeps everything up to a crash.
"""

import json
import os
import threading
from datetime import datetime


class RunRecord:
    """Appends events to ``run.jsonl``, each marked with its registration."""

    def __init__(self, path, registration):
        self.path = path
        self.registration = registration
        self.lock = threading.Lock()

    def write(self, event, **fields):
        line = {
            "event": event,
            "registration": self.registration,
            "time": datetime.now().isoformat(timespec="milliseconds"),
            **fields,
        }
        text = json.dumps(line, default=repr) + "\n"
        with self.lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())


def described(value):
    """*value* for the record: an array as its shape and type, not its numbers."""
    if hasattr(value, "shape") and hasattr(value, "dtype"):
        return {"array": list(value.shape), "dtype": str(value.dtype)}
    if isinstance(value, dict):
        return {plain_key(key): described(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [described(item) for item in value]
    return value


def plain_key(key):
    """A dictionary key JSON can hold."""
    return key if isinstance(key, (str, int, float, bool)) or key is None else repr(key)
