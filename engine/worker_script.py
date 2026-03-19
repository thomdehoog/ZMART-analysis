"""
Worker Script — Subprocess step runner.

Runs INSIDE a target conda environment. Handles two modes:
  --oneshot:  process one request and exit (spawn-run-exit)
  default:    loop until shutdown sentinel or parent death (persistent)

Self-contained: no imports from the engine package, because it may run
in a different conda environment with a different Python version.

Usage (called by Worker, not directly):
    python worker_script.py --port PORT --authkey HEX --step /path/to/step.py
    python worker_script.py --port PORT --authkey HEX --step /path/to/step.py --oneshot
"""

import argparse
import os
import pickle
import sys
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
    parser.add_argument("--oneshot", action="store_true")
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
            response = ("ok", result)
        except Exception:
            response = ("error", {
                "message": traceback.format_exception_only(*sys.exc_info()[:2])[0].strip(),
                "traceback": traceback.format_exc(),
            })

        conn.send_bytes(pickle.dumps(response, protocol=2))

        if args.oneshot:
            break

    conn.close()


if __name__ == "__main__":
    main()
