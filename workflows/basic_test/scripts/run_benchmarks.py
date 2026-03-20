"""
Performance benchmarks for the v3 pipeline engine.

Measures real wall-clock performance across key scenarios:
  1. Engine overhead          — direct call vs run_pipeline
  2. Concurrency scaling      — 1, 2, 4, 8, 16 concurrent jobs
  3. Warm vs cold workers     — first call vs subsequent calls
  4. Scope collection         — overhead of scope_complete when jobs are done
  5. Thread pool saturation   — more jobs than threads
  6. Data serialization       — cost of large data through workers
  7. Multi-run interleaving   — two runs sharing one engine

Usage:
    python run_benchmarks.py
"""

import sys
import time
import textwrap
import tempfile
import shutil
import atexit
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(ROOT))

WIDTH = 70
_TEMP = tempfile.mkdtemp(prefix="bench_")
atexit.register(shutil.rmtree, _TEMP, True)
_counter = 0


def _next_id():
    global _counter
    _counter += 1
    return _counter


def _temp_step(code, name=None):
    path = Path(_TEMP) / (f"{name}.py" if name else f"step_{_next_id()}.py")
    path.write_text(textwrap.dedent(code))
    return str(path)


def _temp_yaml(content):
    text = textwrap.dedent(content)
    if "functions_dir" not in text:
        d = Path(_TEMP).as_posix()
        header = f'metadata:\n  functions_dir: "{d}"\n'
        if "metadata:" in text:
            text = text.replace("metadata:", header.rstrip("\n"), 1)
        else:
            text = header + text
    path = Path(_TEMP) / f"pipeline_{_next_id()}.yaml"
    path.write_text(text)
    return str(path)


def _fmt(seconds):
    if seconds < 0.001:
        return f"{seconds * 1_000_000:.0f}us"
    if seconds < 1:
        return f"{seconds * 1000:.1f}ms"
    return f"{seconds:.2f}s"


def _bar(value, max_value, width=30):
    filled = int(width * value / max_value) if max_value > 0 else 0
    return "#" * filled + "." * (width - filled)


# ── Benchmarks ───────────────────────────────────────────────


def bench_engine_overhead():
    """Measure engine overhead vs direct function call."""
    _temp_step("def run(pd, **p): pd['x'] = 1; return pd", name="bo_noop")
    yaml = _temp_yaml("wf:\n  - bo_noop:")

    from engine._loader import load_function
    module = load_function("bo_noop", Path(_TEMP))

    # Direct call (baseline)
    n = 100
    t0 = time.perf_counter()
    for _ in range(n):
        module.run({"metadata": {}, "input": {}})
    direct = (time.perf_counter() - t0) / n

    # Via run_pipeline
    from engine import run_pipeline
    n_engine = 10
    t0 = time.perf_counter()
    for _ in range(n_engine):
        run_pipeline(yaml, "t", {})
    via_engine = (time.perf_counter() - t0) / n_engine

    print(f"  Direct call:       {_fmt(direct)}/call ({n} calls)")
    print(f"  Via run_pipeline:  {_fmt(via_engine)}/call ({n_engine} calls)")
    if direct > 0:
        print(f"  Overhead:          {via_engine / direct:.0f}x")
    else:
        print(f"  Overhead:          (direct call too fast to measure)")


def bench_concurrency_scaling():
    """Measure throughput scaling with concurrent jobs."""
    _temp_step("""
        import time
        def run(pd, **p):
            time.sleep(0.1)
            pd["done"] = True
            return pd
    """, name="bs_work")
    yaml = _temp_yaml("wf:\n  - bs_work:")

    from engine import PipelineEngine

    n_jobs = 16
    levels = [1, 2, 4, 8, 16]
    results = {}

    for n_threads in levels:
        with PipelineEngine(max_concurrent=n_threads) as e:
            run = e.create_run(yaml)
            t0 = time.perf_counter()
            futures = [run.submit(f"j{i}", {}) for i in range(n_jobs)]
            for f in futures:
                f.result(timeout=60)
            elapsed = time.perf_counter() - t0
            results[n_threads] = elapsed

    baseline = results[1]
    for n_threads in levels:
        elapsed = results[n_threads]
        speedup = baseline / elapsed
        bar = _bar(speedup, max(baseline / v for v in results.values()))
        print(f"  {n_threads:2d} threads: {_fmt(elapsed):>8s}  "
              f"{speedup:5.1f}x  {bar}")


def bench_warm_worker():
    """Measure cold start vs warm calls on persistent workers."""
    from engine._worker import Worker

    path = _temp_step("""
        def run(pd, **p):
            pd["ok"] = True
            return pd
    """)

    w = Worker("local", oneshot=False, connect_timeout=10)
    try:
        # Cold start
        t0 = time.perf_counter()
        w.execute(path, {}, {}, timeout=10)
        cold = time.perf_counter() - t0

        # Warm calls
        n = 20
        t0 = time.perf_counter()
        for _ in range(n):
            w.execute(path, {}, {}, timeout=10)
        warm = (time.perf_counter() - t0) / n

        print(f"  Cold start:  {_fmt(cold)}")
        print(f"  Warm call:   {_fmt(warm)} (avg of {n})")
        if warm > 0:
            print(f"  Speedup:     {cold / warm:.1f}x")
        else:
            print(f"  Speedup:     (warm call too fast to measure)")
    finally:
        w.shutdown()


def bench_scope_collection():
    """Measure scope_complete latency when all jobs are already done."""
    _temp_step("def run(pd, **p): pd['v'] = pd['input']['v']; return pd",
               name="bsc_a")
    _temp_step("""
        def run(pd, **p):
            pd["n"] = len(pd["results"])
            return pd
    """, name="bsc_b")
    yaml = _temp_yaml("""
        wf:
          - bsc_a:
          - bsc_b:
              scope:
                spatial: r
    """)

    from engine import PipelineEngine

    for n_jobs in [5, 20, 50, 100]:
        with PipelineEngine(max_concurrent=32) as e:
            run = e.create_run(yaml)
            futures = [
                run.submit(f"j{i}", {"v": i}, spatial={"r": "X"})
                for i in range(n_jobs)
            ]
            # Wait for all Phase 0 to complete
            for f in futures:
                f.result(timeout=60)

            # Now measure scope_complete (all jobs already done)
            t0 = time.perf_counter()
            r = run.scope_complete(spatial={"r": "X"}).result(timeout=60)
            scope_time = time.perf_counter() - t0

            assert r["n"] == n_jobs
            print(f"  {n_jobs:3d} jobs: scope_complete in {_fmt(scope_time)}")


def bench_thread_pool_saturation():
    """Measure throughput when jobs exceed max_concurrent."""
    _temp_step("""
        import time
        def run(pd, **p):
            time.sleep(0.05)
            pd["done"] = True
            return pd
    """, name="btp")
    yaml = _temp_yaml("wf:\n  - btp:")

    from engine import PipelineEngine

    n_jobs = 40
    for max_c in [4, 8, 16, 40]:
        with PipelineEngine(max_concurrent=max_c) as e:
            run = e.create_run(yaml)
            t0 = time.perf_counter()
            futures = [run.submit(f"j{i}", {}) for i in range(n_jobs)]
            for f in futures:
                f.result(timeout=60)
            elapsed = time.perf_counter() - t0
            throughput = n_jobs / elapsed

            print(f"  max_concurrent={max_c:2d}: "
                  f"{_fmt(elapsed):>8s} total, "
                  f"{throughput:.1f} jobs/s")


def bench_data_serialization():
    """Measure cost of moving large data through subprocess workers."""
    from engine._worker import Worker

    path = _temp_step("def run(pd, **p): return pd")

    sizes = [100, 1_000, 10_000, 50_000]
    w = Worker("local", oneshot=False, connect_timeout=10)
    try:
        # Warm up
        w.execute(path, {}, {}, timeout=10)

        for n in sizes:
            data = {"big": list(range(n))}
            t0 = time.perf_counter()
            r = w.execute(path, data, {}, timeout=30)
            elapsed = time.perf_counter() - t0
            assert len(r["big"]) == n
            print(f"  {n:>6,d} items: {_fmt(elapsed):>8s}  "
                  f"(round-trip through subprocess)")
    finally:
        w.shutdown()


def bench_multi_run_interleaving():
    """Measure throughput of two interleaved runs sharing one engine."""
    _temp_step("""
        import time
        def run(pd, **p):
            time.sleep(0.05)
            pd["run"] = pd["input"]["run"]
            return pd
    """, name="bmi")
    yaml = _temp_yaml("wf:\n  - bmi:")

    from engine import PipelineEngine

    n_per_run = 10

    # Single run baseline
    with PipelineEngine(max_concurrent=16) as e:
        run = e.create_run(yaml)
        t0 = time.perf_counter()
        futures = [run.submit(f"j{i}", {"run": "A"}) for i in range(n_per_run)]
        for f in futures:
            f.result(timeout=60)
        single = time.perf_counter() - t0

    # Two runs interleaved
    with PipelineEngine(max_concurrent=16) as e:
        run_a = e.create_run(yaml, priority="high")
        run_b = e.create_run(yaml)
        t0 = time.perf_counter()
        futures = []
        for i in range(n_per_run):
            futures.append(run_a.submit(f"a{i}", {"run": "A"}))
            futures.append(run_b.submit(f"b{i}", {"run": "B"}))
        for f in futures:
            f.result(timeout=60)
        dual = time.perf_counter() - t0

    print(f"  Single run ({n_per_run} jobs):     {_fmt(single)}")
    print(f"  Two runs ({n_per_run * 2} jobs):    {_fmt(dual)}")
    print(f"  Overhead:                    "
          f"{((dual / single) - 1) * 100:.0f}% "
          f"(for 2x the work)")


# ── Main ──────────────────────────────────────────────────────


BENCHMARKS = [
    ("Engine overhead", bench_engine_overhead),
    ("Concurrency scaling", bench_concurrency_scaling),
    ("Warm worker advantage", bench_warm_worker),
    ("Scope collection latency", bench_scope_collection),
    ("Thread pool saturation", bench_thread_pool_saturation),
    ("Data serialization cost", bench_data_serialization),
    ("Multi-run interleaving", bench_multi_run_interleaving),
]


def main():
    import engine
    print()
    print("=" * WIDTH)
    print("  SMART Analysis v3 — Performance Benchmarks")
    print("=" * WIDTH)
    print()
    print(f"  Engine:   {engine.__version__}")
    print(f"  Python:   {sys.version.split()[0]}")
    print()

    t_total = time.perf_counter()

    for name, func in BENCHMARKS:
        print("-" * WIDTH)
        print(f"  {name}")
        print("-" * WIDTH)
        try:
            func()
        except Exception as e:
            print(f"  ERROR: {e}")
        print()

    elapsed = time.perf_counter() - t_total
    print("=" * WIDTH)
    print(f"  Total benchmark time: {_fmt(elapsed)}")
    print("=" * WIDTH)


if __name__ == "__main__":
    main()
