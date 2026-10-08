"""
Worker subprocess script -- runs inside the target conda environment.

Self-contained: imports nothing from the engine package. It may run in a
completely different conda environment with different packages and even a
different Python version than the orchestrator.

Protocol
--------
1. Parent spawns this script with --port, --authkey
2. Script connects to parent on localhost:port with authkey
3. Message loop:
   - Receive (step_path, pipeline_data, params)
   - Load module at step_path (cached)
   - Call module.run(pipeline_data, state, **params)
   - Send ("ok", result) or ("error", {"message": ..., "traceback": ...})
4. Receive None sentinel -> clean exit

State management
----------------
Each step gets its own persistent state dict, keyed by step path. The state
dict is passed to run() on every call. First call sees an empty dict. The
step populates it with whatever it needs (models, caches, etc.). State is
garbage collected when the worker shuts down.

Module caching
--------------
Modules are loaded on first use and cached by path. A persistent worker
executing the same step repeatedly pays the import cost only once. Different
steps in the same environment load fresh but share the process. A step is
loaded as a real module, registered under its file name in ``sys.modules``,
so everything that looks a module up by name (dataclasses, pickling, a
sibling step importing it) finds it.

Orphan detection
----------------
Persistent workers periodically check if the engine process is alive.
If the engine dies, the worker exits cleanly rather than becoming an orphan.
The engine passes its own PID via --parent-pid: os.getppid() cannot be
used because conda-env workers are spawned through a `conda run` wrapper
process, so the worker's direct parent is the wrapper, not the engine.
The wrapper stays alive waiting on the worker even after the engine dies,
which would defeat the check.

Usage (called by Worker, not directly)
--------------------------------------
    python worker_script.py --port PORT --authkey HEX --parent-pid PID
"""

import argparse
import hashlib
import importlib.util
import logging
import os
import pickle
import platform
import sys
import traceback
from importlib import metadata
from multiprocessing.connection import Client

#: How the messages on the socket are encoded. Protocol 5 is what every
#: Python from 3.8 on reads, and it carries a large image in one piece
#: instead of the slow, chunked form of the old protocol 2. The engine
#: uses the same number (see workers.py).
PICKLE_PROTOCOL = 5


def _load_module(step_path):
    """Import the step file as a module named after it, and return it.

    The engine itself never runs the file; it only reads its METADATA
    (engine.get_step_settings). The running happens here, in the step's
    own environment. The module goes into ``sys.modules`` under the file's
    name, as an ordinary import would put it, because several things look
    a module up by its name: a dataclass resolving its annotations, pickle
    sending a class defined in the step back to the engine, a sibling step
    importing this one. When another file already holds that name, this
    one gets the name with a short hash of its path on the end, so neither
    shadows the other.
    """
    name = os.path.splitext(os.path.basename(step_path))[0]
    taken = sys.modules.get(name)
    if taken is not None and getattr(taken, "__file__", None) != step_path:
        name = f"{name}_{hashlib.sha256(step_path.encode()).hexdigest()[:8]}"
    spec = importlib.util.spec_from_file_location(name, step_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _environment_record():
    """What this worker's environment is, measured once when it starts.

    ``environment`` is the conda environment's name, ``python`` the
    interpreter version, and ``fingerprint`` a short hash of every installed
    package and its version. Two results with the same fingerprint ran on
    identical environments; a changed fingerprint means something was
    installed, removed or upgraded in between.
    """
    installed = sorted(
        f"{(dist.metadata['Name'] or '').lower()}=={dist.version}"
        for dist in metadata.distributions()
    )
    return {
        "environment": os.environ.get("CONDA_DEFAULT_ENV") or os.path.basename(sys.prefix),
        "python": platform.python_version(),
        "fingerprint": hashlib.sha256("\n".join(installed).encode()).hexdigest()[:16],
    }


def _loaded_package_versions(module_to_dists):
    """The version of every installed package this worker has imported.

    Only what was actually loaded, so a step's record names cellpose and
    torch and numpy, not the two hundred packages that sit unused in the
    environment. The standard library is not a package and is not listed.
    """
    versions = {}
    for top in {name.split(".")[0] for name in list(sys.modules)}:
        for dist in module_to_dists.get(top, ()):
            try:
                versions[dist.lower()] = metadata.version(dist)
            except metadata.PackageNotFoundError:
                pass
    return dict(sorted(versions.items()))


def _stamp(result, step_name, env_record, module_to_dists):
    """Add this step's provenance to its result, when the result is a dict."""
    if isinstance(result, dict):
        record = dict(env_record)
        record["packages"] = _loaded_package_versions(module_to_dists)
        result.setdefault("provenance", {})[step_name] = record
    return result


def _parent_alive(parent_pid):
    """Check if the parent process is still running.

    On Unix, os.kill(pid, 0) tests existence without sending a signal.
    On Windows, signal 0 maps to CTRL_C_EVENT which would interrupt the
    parent, so we use kernel32.OpenProcess instead.
    """
    if sys.platform == "win32":
        return _windows_process_alive(parent_pid)
    try:
        os.kill(parent_pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _windows_process_alive(parent_pid, kernel32=None):
    """Return whether a Windows process exists and has not terminated.

    ``OpenProcess`` can succeed for a terminated process while another handle
    still keeps its kernel object alive. Query the process handle's signaled
    state as well: process handles become signaled when the process exits.
    ``kernel32`` is injectable so this behavior is testable on every CI OS.
    """
    if kernel32 is None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = (
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
        )
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

    synchronize = 0x00100000
    wait_timeout = 0x00000102
    handle = kernel32.OpenProcess(synchronize, False, parent_pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
    finally:
        kernel32.CloseHandle(handle)


def main():
    parser = argparse.ArgumentParser(description="Pipeline engine worker")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--authkey", required=True)
    parser.add_argument(
        "--parent-pid", type=int, default=None, help="Engine PID to watch; defaults to os.getppid()"
    )
    args = parser.parse_args()

    log_level = os.environ.get("ZMART_LOG_LEVEL", "WARNING")
    logging.basicConfig(
        level=getattr(logging, log_level, logging.WARNING),
        format="[worker %(process)d] %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    logger = logging.getLogger("engine.worker")

    parent_pid = args.parent_pid if args.parent_pid is not None else os.getppid()
    logger.info("Worker starting: pid=%d, port=%d, parent=%d", os.getpid(), args.port, parent_pid)

    conn = Client(("localhost", args.port), authkey=bytes.fromhex(args.authkey))
    logger.info("Connected to parent on port %d", args.port)

    module_cache = {}
    state_dicts = {}
    request_count = 0
    # Measured once per worker: the environment cannot change under a
    # running interpreter in any way that would matter to the record.
    env_record = _environment_record()
    module_to_dists = metadata.packages_distributions()

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
            logger.info("Request #%d: step=%s (%d bytes)", request_count, step_name, len(raw))

            # Load or reuse cached module
            if step_path not in module_cache:
                logger.info("Loading module: %s", step_name)
                module_cache[step_path] = _load_module(step_path)
            module = module_cache[step_path]

            # Get or create per-step state dict
            if step_path not in state_dicts:
                state_dicts[step_path] = {}
            state = state_dicts[step_path]

            try:
                result = module.run(pipeline_data, state, **params)
                result = _stamp(result, os.path.splitext(step_name)[0], env_record, module_to_dists)
                response = ("ok", result)
                logger.info("Request #%d completed", request_count)
            except Exception:
                tb = traceback.format_exc()
                logger.error("Request #%d failed:\n%s", request_count, tb)
                response = (
                    "error",
                    {
                        "message": traceback.format_exception_only(*sys.exc_info()[:2])[0].strip(),
                        "traceback": tb,
                    },
                )

            conn.send_bytes(pickle.dumps(response, protocol=PICKLE_PROTOCOL))
    finally:
        conn.close()
        logger.info("Worker exiting: pid=%d, requests=%d", os.getpid(), request_count)


if __name__ == "__main__":
    main()
