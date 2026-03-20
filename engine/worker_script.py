"""
Worker subprocess script — runs inside the target conda environment.

Self-contained: imports nothing from the engine package. It may run in a
completely different conda environment with different packages and even a
different Python version than the orchestrator.

v3 protocol change: the worker is per-environment, not per-step. Each
message includes the step path, and modules are loaded on demand and cached.
This lets one worker execute any step that runs in its environment.

Protocol
--------
1. Parent spawns this script with --port, --authkey, [--oneshot]
2. Script connects to parent on localhost:port with authkey
3. Message loop:
   - Receive (step_path, pipeline_data, params)
   - Load module at step_path (cached for warm workers)
   - Call module.run(pipeline_data, **params)
   - Send ("ok", result) or ("error", {"message": ..., "traceback": ...})
4. Receive None sentinel → clean exit

Module caching
--------------
Modules are loaded on first use and cached by path. A persistent worker
executing the same step repeatedly pays the import cost only once. Different
steps in the same environment load fresh but share the process — no
serialization overhead between them.

Orphan detection
----------------
Persistent workers periodically check if the parent process is alive.
If the parent dies (e.g., KeyboardInterrupt), the worker exits cleanly
rather than becoming an orphan.

Usage (called by Worker, not directly)
--------------------------------------
    python worker_script.py --port PORT --authkey HEX
    python worker_script.py --port PORT --authkey HEX --oneshot
"""

import argparse
import logging
import os
import pickle
import sys
import traceback
import types
from multiprocessing.connection import Client


def _load_module(step_path):
    """Load a step module via exec. Mirrors _loader.load_function."""
    name = os.path.splitext(os.path.basename(step_path))[0]
    namespace = {"__name__": name, "__file__": step_path}
    with open(step_path) as f:
        exec(compile(f.read(), step_path, "exec"), namespace)
    module = types.ModuleType(name)
    module.__dict__.update(namespace)
    return module


def _parent_alive(parent_pid):
    """Check if the parent process is still running.

    On Unix, os.kill(pid, 0) tests existence without sending a signal.
    On Windows, signal 0 maps to CTRL_C_EVENT which would interrupt the
    parent, so we use kernel32.OpenProcess instead.
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
    parser = argparse.ArgumentParser(description="Pipeline engine worker")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--authkey", required=True)
    parser.add_argument("--oneshot", action="store_true")
    args = parser.parse_args()

    log_level = os.environ.get("SMART_LOG_LEVEL", "WARNING")
    logging.basicConfig(
        level=getattr(logging, log_level, logging.WARNING),
        format="[worker %(process)d] %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    logger = logging.getLogger("engine.worker")

    parent_pid = os.getppid()
    logger.info("Worker starting: pid=%d, port=%d, oneshot=%s, parent=%d",
                os.getpid(), args.port, args.oneshot, parent_pid)

    conn = Client(("localhost", args.port),
                  authkey=bytes.fromhex(args.authkey))
    logger.info("Connected to parent on port %d", args.port)

    module_cache = {}
    request_count = 0

    try:
        while True:
            if not _parent_alive(parent_pid):
                logger.warning("Parent %d died, shutting down", parent_pid)
                break

            if not conn.poll(timeout=5.0):
                continue

            raw = conn.recv_bytes()
            message = pickle.loads(raw)

            if message is None:
                logger.info("Received shutdown sentinel")
                break

            step_path, pipeline_data, params = message
            step_name = os.path.basename(step_path)
            request_count += 1
            logger.info("Request #%d: step=%s (%d bytes)",
                        request_count, step_name, len(raw))

            # Load or reuse cached module
            if step_path not in module_cache:
                logger.info("Loading module: %s", step_name)
                module_cache[step_path] = _load_module(step_path)
            module = module_cache[step_path]

            try:
                result = module.run(pipeline_data, **params)
                response = ("ok", result)
                logger.info("Request #%d completed", request_count)
            except Exception:
                tb = traceback.format_exc()
                logger.error("Request #%d failed:\n%s", request_count, tb)
                response = ("error", {
                    "message": traceback.format_exception_only(
                        *sys.exc_info()[:2])[0].strip(),
                    "traceback": tb,
                })

            conn.send_bytes(pickle.dumps(response, protocol=2))

            if args.oneshot:
                logger.info("Oneshot mode, exiting")
                break
    finally:
        conn.close()
        logger.info("Worker exiting: pid=%d, requests=%d",
                    os.getpid(), request_count)


if __name__ == "__main__":
    main()
