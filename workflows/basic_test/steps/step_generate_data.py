"""
Benchmark step — generate data of configurable size.

Creates a list of dicts simulating tile/feature data. Used to measure
serialization overhead at scope boundaries and env crossings.
"""

METADATA = {
    "description": "Data generation benchmark",
    "environment": "local",
}


def run(pipeline_data, **params):
    import time

    n_items = params.get("n_items", 100)
    item_size = params.get("item_size", 10)
    t0 = time.monotonic()

    data = [
        {"id": i, "values": list(range(item_size)), "label": f"item_{i}"}
        for i in range(n_items)
    ]

    elapsed = time.monotonic() - t0
    pipeline_data["generated"] = {
        "data": data,
        "n_items": n_items,
        "elapsed": elapsed,
    }
    return pipeline_data
