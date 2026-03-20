"""
Benchmark step — configurable CPU workload.

Simulates real computation (matrix multiply) with tunable duration
via the 'size' parameter. Used to measure engine overhead vs actual work.
"""

METADATA = {
    "description": "CPU workload benchmark",
    "environment": "local",
}


def run(pipeline_data, **params):
    import time

    size = params.get("size", 100)
    t0 = time.monotonic()

    # Real CPU work: nested sum to simulate matrix operations
    total = sum(i * j for i in range(size) for j in range(size))

    elapsed = time.monotonic() - t0
    pipeline_data["cpu_work"] = {
        "size": size,
        "result": total,
        "elapsed": elapsed,
    }
    return pipeline_data
