"""
Test step -- sleeps for a configurable duration.

For testing timeouts and concurrent behavior. The delay parameter
controls sleep duration in seconds (default 0.5).
"""

METADATA = {
    "description": "Configurable delay step for concurrency tests",
    "version": "1.0",
    "environment": "local",
}


def run(pipeline_data: dict, **params) -> dict:
    import os
    import sys
    import time

    delay = params.get("delay", 0.5)
    verbose = pipeline_data["metadata"].get("verbose", 0)

    if verbose >= 2:
        print()
        print("=" * 50)
        print("  STEP: step_slow")
        print("=" * 50)
        print(f"    Sleeping for {delay}s")

    t0 = time.perf_counter()
    time.sleep(delay)
    elapsed = time.perf_counter() - t0

    pipeline_data["step_slow"] = {
        "executed": True,
        "requested_delay": delay,
        "actual_delay": elapsed,
        "environment": os.path.basename(sys.prefix),
        "process_id": os.getpid(),
    }

    return pipeline_data
