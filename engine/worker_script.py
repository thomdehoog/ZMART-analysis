"""
Worker Script — Subprocess step runner.

This script runs INSIDE a target conda environment as a child process of
Worker (_worker.py). It loads a step module, connects back to the parent
via TCP, and processes requests in a loop.

IMPORTANT: This file is self-contained — no imports from the engine package.
It may run in a completely different conda environment with different packages
and even a different Python version than the orchestrator.

Two modes
---------
--oneshot   Process one request and exit. Used for spawn-run-exit steps.
(default)   Loop until shutdown sentinel or parent death. Used for persistent
            workers that keep ML models warm in memory.

Protocol
--------
1. Parent spawns this script with --port, --authkey, --step [--oneshot]
2. Script connects to parent on localhost:port with authkey
3. Script loads the step module via exec()
4. Loop: receive (pipeline_data, params) → call module.run() → send response
5. Exit on: None sentinel, parent death, or oneshot completion

Logging
-------
Logs to stderr at WARNING+ by default (to avoid filling the pipe buffer
on persistent workers). Set SMART_LOG_LEVEL=DEBUG in the environment for
verbose output during troubleshooting.

Usage (called by Worker, not directly)
--------------------------------------
    python worker_script.py --port PORT --authkey HEX --step /path/to/step.py
    python worker_script.py --port PORT --authkey HEX --step /path/to/step.py --oneshot
"""

import argparse
import logging
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
    name = os.path.splitext(os.path.basename(step_path))[0]
    namespace = {"__name__": name, "__file__": step_path}
    with open(step_path) as f:
        exec(compile(f.read(), step_path, "exec"), namespace)

    module = types.ModuleType(name)
    module.__dict__.update(namespace)
    return module


def parent_alive(parent_pid):
    """Check if the parent process is still running.

    On Unix, os.kill(pid, 0) is the standard check — signal 0 tests
    process existence without sending a real signal.

    On Windows, signal 0 maps to CTRL_C_EVENT, which would send an
    actual interrupt to the parent's console group. Instead, use
    kernel32.OpenProcess to test process existence without side effects.
    """
    if sys.platform == "win32":
        import ctypes
        SYNCHRONIZE = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(
            SYNCHRONIZE, False, parent_pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
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

    # Logging to stderr — WARNING+ by default to avoid filling the pipe
    # buffer on persistent workers. Set SMART_LOG_LEVEL=DEBUG for verbose.
    log_level = os.environ.get("SMART_LOG_LEVEL", "WARNING")
    logging.basicConfig(
        level=getattr(logging, log_level, logging.WARNING),
        format="[worker %(process)d] %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    logger = logging.getLogger("engine.worker")

    parent_pid = os.getppid()
    step_name = os.path.basename(args.step)

    logger.info("Worker starting: pid=%d, step=%s, port=%d, oneshot=%s, "
                "parent_pid=%d", os.getpid(), step_name, args.port,
                args.oneshot, parent_pid)

    # Connect to parent
    authkey = bytes.fromhex(args.authkey)
    conn = Client(("localhost", args.port), authkey=authkey)
    logger.info("Connected to parent on port %d", args.port)

    # Load step module
    try:
        module = load_step_module(args.step)
        logger.info("Step module loaded: %s", step_name)
    except Exception:
        logger.error("Failed to load step module %s:\n%s",
                     step_name, traceback.format_exc())
        raise

    # Main loop
    request_count = 0
    while True:
        # Check for orphan (parent died)
        if not parent_alive(parent_pid):
            logger.warning("Parent process %d died, shutting down", parent_pid)
            break

        # Wait for work with timeout so we can periodically check parent
        if not conn.poll(timeout=5.0):
            continue

        # Receive message
        raw = conn.recv_bytes()
        message = pickle.loads(raw)

        # Shutdown sentinel
        if message is None:
            logger.info("Received shutdown sentinel")
            break

        pipeline_data, params = message
        request_count += 1
        logger.info("Request #%d received (%d bytes)", request_count, len(raw))

        try:
            result = module.run(pipeline_data, **params)
            response = ("ok", result)
            logger.info("Request #%d completed successfully", request_count)
        except Exception:
            tb = traceback.format_exc()
            logger.error("Request #%d failed:\n%s", request_count, tb)
            response = ("error", {
                "message": traceback.format_exception_only(*sys.exc_info()[:2])[0].strip(),
                "traceback": tb,
            })

        conn.send_bytes(pickle.dumps(response, protocol=2))

        if args.oneshot:
            logger.info("Oneshot mode, exiting after single request")
            break

    conn.close()
    logger.info("Worker exiting: pid=%d, requests_processed=%d",
                os.getpid(), request_count)


if __name__ == "__main__":
    main()
