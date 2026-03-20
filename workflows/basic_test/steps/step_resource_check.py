"""
Test step -- reports process resource information.

Captures PID and file descriptor count for cleanup verification.
Handles platform differences (Linux /proc/self/fd vs others).
"""

METADATA = {
    "description": "Report PID and file descriptors for cleanup tests",
    "version": "1.0",
    "environment": "local",
}


def run(pipeline_data: dict, **params) -> dict:
    import os
    import sys
    import platform

    verbose = pipeline_data["metadata"].get("verbose", 0)

    pid = os.getpid()
    fd_count = None

    if platform.system() == "Linux":
        proc_fd = "/proc/self/fd"
        if os.path.isdir(proc_fd):
            fd_count = len(os.listdir(proc_fd))
    elif platform.system() == "Windows":
        # No portable FD count on Windows; report None
        fd_count = None
    elif platform.system() == "Darwin":
        # macOS: no /proc, report None
        fd_count = None

    if verbose >= 2:
        print()
        print("=" * 50)
        print("  STEP: step_resource_check")
        print("=" * 50)
        print(f"    PID: {pid}")
        print(f"    Platform: {platform.system()}")
        print(f"    FD count: {fd_count}")

    pipeline_data["resource_check"] = {
        "pid": pid,
        "platform": platform.system(),
        "fd_count": fd_count,
        "python_executable": sys.executable,
    }

    return pipeline_data
