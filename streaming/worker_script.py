"""
Worker Script — Persistent subprocess loop

This script runs INSIDE a target conda environment. It is spawned once by the
Worker class and stays alive, processing requests until it receives a shutdown
sentinel or the parent process dies.

It is fully self-contained: no imports from the streaming package, because it
may run in a different conda environment with a different Python version.

Usage (called by Worker, not directly):
    python worker_script.py --port PORT --authkey HEX --step /path/to/step.py
"""

import argparse
import os
import pickle
import sys
import time
import traceback
import types
from multiprocessing.connection import Client


def load_step_module(step_path):
    """
    Load a step module using exec-based loading.

    Same approach as engine.py to avoid Windows DLL search path issues
    that can break packages like PyTorch on network drives.
    """
    namespace = {"__name__": os.path.basename(step_path), "__file__": step_path}
    with open(step_path) as f:
        exec(compile(f.read(), step_path, "exec"), namespace)

    module = types.ModuleType(os.path.basename(step_path))
    module.__dict__.update(namespace)
    return module


def parent_alive(parent_pid):
    """Check if the parent process is still running."""
    try:
        os.kill(parent_pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--authkey", type=str, required=True)
    parser.add_argument("--step", type=str, required=True)
    args = parser.parse_args()

    parent_pid = os.getppid()

    # Connect to parent
    authkey = bytes.fromhex(args.authkey)
    conn = Client(("localhost", args.port), authkey=authkey)

    # Load step module
    module = load_step_module(args.step)

    # Main loop
    while True:
        # Check for orphan (parent died)
        if not parent_alive(parent_pid):
            break

        # Wait for work with timeout so we can periodically check parent
        if not conn.poll(timeout=5.0):
            continue

        # Receive message
        raw = conn.recv_bytes()
        message = pickle.loads(raw)

        # Shutdown sentinel
        if message is None:
            break

        pipeline_data, params = message

        try:
            result = module.run(pipeline_data, **params)
            response = result
        except Exception:
            response = {
                "__error__": True,
                "message": traceback.format_exception_only(*sys.exc_info()[:2])[0].strip(),
                "traceback": traceback.format_exc(),
            }

        conn.send_bytes(pickle.dumps(response, protocol=2))

    conn.close()


if __name__ == "__main__":
    main()
