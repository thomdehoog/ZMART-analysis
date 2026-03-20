"""
Performance benchmarks for the rare_event_selection workflow.

Measures real wall-clock performance across the analysis pipeline:
  1. End-to-end pipeline        — full run_pipeline() cold vs warm
  2. Per-step breakdown         — time spent in each step individually
  3. Cellpose cold vs warm      — model load vs warm inference
  4. Device comparison          — compare available devices (CPU, GPU)
  5. Parallel segmentation      — concurrent workers sharing VRAM

Each benchmark auto-detects the available device and adapts accordingly.

Usage:
    python run_benchmarks.py
    python run_benchmarks.py --source path/to/image.tif
    python run_benchmarks.py --workers 3  # parallel workers (default: 3)
"""

import sys
import time
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(ROOT))

WORKFLOW_DIR = Path(__file__).parent.parent
STEPS_DIR = WORKFLOW_DIR / "steps"
YAML_PATH = WORKFLOW_DIR / "pipelines" / "rare_event_selection_pipeline.yaml"

WIDTH = 70


# ── Formatting ───────────────────────────────────────────────


def _fmt(seconds):
    if seconds < 0.001:
        return f"{seconds * 1_000_000:.0f}us"
    if seconds < 1:
        return f"{seconds * 1000:.1f}ms"
    return f"{seconds:.2f}s"


def _bar(value, max_value, width=30):
    filled = int(width * value / max_value) if max_value > 0 else 0
    return "#" * filled + "." * (width - filled)


def _banner(title):
    print()
    print("=" * WIDTH)
    print(f"  {title}")
    print("=" * WIDTH)


def _section(title):
    print()
    print("-" * WIDTH)
    print(f"  {title}")
    print("-" * WIDTH)


# ── Device detection ─────────────────────────────────────────


def _detect_devices():
    """Detect all available compute devices.

    Returns a list of (label, use_gpu) tuples for each available device.
    CPU is always available.
    """
    devices = [("CPU", False)]

    try:
        import torch
        if torch.cuda.is_available():
            name = torch.cuda.get_device_name(0)
            vram = torch.cuda.get_device_properties(0).total_mem / (1024**3)
            devices.append((f"GPU — {name} ({vram:.1f} GB)", True))
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            devices.append(("GPU — Apple MPS", True))
    except ImportError:
        pass

    return devices


def _get_vram_gb():
    """Return total VRAM in GB, or None if unavailable."""
    try:
        import torch
        if torch.cuda.is_available():
            return torch.cuda.get_device_properties(0).total_mem / (1024**3)
    except ImportError:
        pass
    return None


def _has_gpu():
    """Check if any GPU device is available."""
    return any(use_gpu for _, use_gpu in _detect_devices())


# ── Helpers ──────────────────────────────────────────────────


def _load_image(data_source):
    """Load image for benchmarks."""
    if data_source == "skimage.human_mitosis":
        from skimage.data import human_mitosis
        return human_mitosis()
    else:
        from skimage.io import imread
        return imread(data_source)


def _preprocess_image(img):
    """Apply standard preprocessing to an image."""
    import numpy as np
    from skimage.filters import gaussian
    from skimage.exposure import equalize_adapthist

    img_smooth = gaussian(img, sigma=1.0)
    img_pre = equalize_adapthist(img_smooth, clip_limit=0.03)
    return (img_pre * 255).astype(np.uint8)


def _make_pipeline_data(label="bench", data_source="skimage.human_mitosis"):
    """Create a minimal pipeline_data dict for step-level benchmarks."""
    return {
        "metadata": {
            "label": label,
            "datetime": "bench",
            "workflow_name": "rare-event-selection",
            "yaml_filename": "benchmark",
            "steps": [],
            "verbose": 0,
        },
        "input": {"data_source": data_source},
    }


def _run_step(step_name, pipeline_data, **params):
    """Load and run a single step, returning (elapsed, pipeline_data)."""
    from engine._loader import load_function
    module = load_function(step_name, STEPS_DIR)
    t0 = time.perf_counter()
    result = module.run(pipeline_data, **params)
    elapsed = time.perf_counter() - t0
    return elapsed, result


# ── Benchmarks ───────────────────────────────────────────────


def bench_end_to_end(data_source, **_):
    """Full pipeline via run_pipeline() — cold vs warm."""
    from engine import run_pipeline

    times = []
    for i in range(3):
        t0 = time.perf_counter()
        result = run_pipeline(
            str(YAML_PATH), f"bench_{i}",
            {"data_source": data_source},
        )
        elapsed = time.perf_counter() - t0
        times.append(elapsed)

    n_cells = result["segment"]["n_cells"]
    n_selected = result["feedback"]["n_selected"]

    print(f"  Cells segmented:  {n_cells}")
    print(f"  Cells selected:   {n_selected}")
    print()
    for i, t in enumerate(times):
        label = "cold" if i == 0 else f"warm {i}"
        bar = _bar(t, max(times))
        print(f"  Run {i+1} ({label:6s}): {_fmt(t):>8s}  {bar}")
    print()
    if len(times) > 1:
        print(f"  Cold/warm ratio:  {times[0] / times[1]:.2f}x")


def bench_per_step(data_source, **_):
    """Time each step individually."""
    steps = [
        ("preprocess", {"sigma": 1.0, "clip_limit": 0.03}),
        ("segment",    {"diameter": None, "gpu": False}),
        ("extract_features", {"select_by": "area", "percentile": 99}),
        ("feedback",   {"output_dir": "/tmp/smart_bench"}),
    ]

    pd = _make_pipeline_data(data_source=data_source)
    timings = {}

    for step_name, params in steps:
        elapsed, pd = _run_step(step_name, pd, **params)
        timings[step_name] = elapsed

    total = sum(timings.values())
    for name, t in timings.items():
        pct = (t / total) * 100
        bar = _bar(t, max(timings.values()))
        print(f"  {name:<20s} {_fmt(t):>8s}  {pct:5.1f}%  {bar}")
    print()
    print(f"  {'Total':<20s} {_fmt(total):>8s}")


def bench_cold_warm(data_source, **_):
    """Measure Cellpose cold vs warm across all available devices."""
    from cellpose import models

    img = _load_image(data_source)
    img_pre = _preprocess_image(img)

    devices = _detect_devices()

    for device_label, use_gpu in devices:
        print(f"  {device_label}:")

        # Cold: model load
        t0 = time.perf_counter()
        model = models.CellposeModel(gpu=use_gpu)
        t_load = time.perf_counter() - t0

        # First inference
        t0 = time.perf_counter()
        masks, _, _ = model.eval(img_pre, diameter=None)
        t_first = time.perf_counter() - t0

        # Warm runs
        warm_times = []
        for _ in range(3):
            t0 = time.perf_counter()
            masks, _, _ = model.eval(img_pre, diameter=None)
            warm_times.append(time.perf_counter() - t0)

        avg_warm = sum(warm_times) / len(warm_times)
        cold_total = t_load + t_first

        print(f"    Model load:          {_fmt(t_load)}")
        print(f"    First inference:     {_fmt(t_first)}")
        print(f"    Warm inference:      {_fmt(avg_warm)}  (avg of 3)")
        print(f"    Cold/warm ratio:     {cold_total / avg_warm:.2f}x")
        print(f"    Savings per warm:    {_fmt(cold_total - avg_warm)}")
        print(f"    Break-even:          "
              f"{int(cold_total / avg_warm) if avg_warm > 0 else '?'} calls")

        del model
        print()


def bench_device_comparison(data_source, **_):
    """Compare segmentation across all available devices."""
    from cellpose import models

    img = _load_image(data_source)
    img_pre = _preprocess_image(img)

    devices = _detect_devices()
    if len(devices) < 2:
        print("  Only one device available — nothing to compare.")
        return

    results = {}

    for device_label, use_gpu in devices:
        model = models.CellposeModel(gpu=use_gpu)

        # Cold
        t0 = time.perf_counter()
        masks, _, _ = model.eval(img_pre, diameter=None)
        cold = time.perf_counter() - t0
        n_cells = int(masks.max())

        # Warm
        warm_times = []
        for _ in range(3):
            t0 = time.perf_counter()
            masks, _, _ = model.eval(img_pre, diameter=None)
            warm_times.append(time.perf_counter() - t0)
        avg_warm = sum(warm_times) / len(warm_times)

        short_label = "CPU" if not use_gpu else "GPU"
        results[short_label] = {"cold": cold, "warm": avg_warm,
                                "n_cells": n_cells}
        print(f"  {device_label}:")
        print(f"    Cold:   {_fmt(cold):>8s}    Cells: {n_cells}")
        print(f"    Warm:   {_fmt(avg_warm):>8s}")

        del model
        print()

    if "CPU" in results and "GPU" in results:
        speedup_warm = results["CPU"]["warm"] / results["GPU"]["warm"]
        speedup_cold = results["CPU"]["cold"] / results["GPU"]["cold"]
        print(f"  Speedup (warm):  {speedup_warm:.2f}x")
        print(f"  Speedup (cold):  {speedup_cold:.2f}x")


def bench_parallel(data_source, n_workers=3, **_):
    """Concurrent segmentation workers sharing the same device.

    Tests parallel throughput by running N workers simultaneously.
    On GPU systems this tests VRAM sharing; on CPU systems it tests
    core utilisation.
    """
    from cellpose import models

    img = _load_image(data_source)
    img_pre = _preprocess_image(img)

    use_gpu = _has_gpu()
    device_label = "GPU" if use_gpu else "CPU"

    vram = _get_vram_gb()
    if vram is not None:
        print(f"  VRAM total:           {vram:.1f} GB")
        print(f"  VRAM per worker:      ~{vram / n_workers:.1f} GB")
        print()

    # --- Sequential baseline ---
    print(f"  Sequential baseline ({device_label}, {n_workers} runs):")

    model = models.CellposeModel(gpu=use_gpu)
    seq_times = []
    for _ in range(n_workers):
        t0 = time.perf_counter()
        masks, _, _ = model.eval(img_pre, diameter=None)
        seq_times.append(time.perf_counter() - t0)

    seq_avg = sum(seq_times) / len(seq_times)
    seq_total = sum(seq_times)
    print(f"    Per-image (avg):    {_fmt(seq_avg)}")
    print(f"    Total:              {_fmt(seq_total)}")
    del model
    print()

    # --- Parallel cold start ---
    def _segment_cold(worker_id, image):
        t0 = time.perf_counter()
        mdl = models.CellposeModel(gpu=use_gpu)
        t_load = time.perf_counter() - t0

        t0 = time.perf_counter()
        masks, _, _ = mdl.eval(image, diameter=None)
        t_infer = time.perf_counter() - t0

        return {
            "worker": worker_id,
            "load": t_load,
            "infer": t_infer,
            "total": t_load + t_infer,
            "n_cells": int(masks.max()),
        }

    print(f"  Parallel cold ({n_workers} workers, 1 image each):")

    t_wall_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = {
            pool.submit(_segment_cold, i, img_pre.copy()): i
            for i in range(n_workers)
        }
        cold_results = {}
        for f in as_completed(futures):
            wid = futures[f]
            try:
                cold_results[wid] = f.result()
            except Exception as e:
                cold_results[wid] = {"error": str(e)}
    t_wall_cold = time.perf_counter() - t_wall_start

    all_ok = True
    for wid in sorted(cold_results):
        r = cold_results[wid]
        if "error" in r:
            print(f"    Worker {wid}: FAILED — {r['error']}")
            all_ok = False
        else:
            print(f"    Worker {wid}: load={_fmt(r['load']):>8s}  "
                  f"infer={_fmt(r['infer']):>8s}  "
                  f"total={_fmt(r['total']):>8s}  "
                  f"cells={r['n_cells']}")

    print()
    print(f"    Wall clock:         {_fmt(t_wall_cold)}")

    if all_ok:
        speedup = seq_total / t_wall_cold if t_wall_cold > 0 else 0
        efficiency = speedup / n_workers * 100
        print(f"    Sequential total:   {_fmt(seq_total)}")
        print(f"    Speedup:            {speedup:.2f}x")
        print(f"    Efficiency:         {efficiency:.0f}% "
              f"(ideal = {n_workers}x)")
    print()

    # --- Parallel warm ---
    print(f"  Parallel warm ({n_workers} workers, 3 rounds each):")

    loaded_models = [models.CellposeModel(gpu=use_gpu)
                     for _ in range(n_workers)]

    def _segment_warm(worker_id, image, mdl):
        times = []
        for _ in range(3):
            t0 = time.perf_counter()
            masks, _, _ = mdl.eval(image, diameter=None)
            times.append(time.perf_counter() - t0)
        return {
            "worker": worker_id,
            "times": times,
            "avg": sum(times) / len(times),
            "n_cells": int(masks.max()),
        }

    t_wall_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = {
            pool.submit(_segment_warm, i, img_pre.copy(), loaded_models[i]): i
            for i in range(n_workers)
        }
        warm_results = {}
        for f in as_completed(futures):
            wid = futures[f]
            try:
                warm_results[wid] = f.result()
            except Exception as e:
                warm_results[wid] = {"error": str(e)}
    t_wall_warm = time.perf_counter() - t_wall_start

    total_images = n_workers * 3
    seq_equivalent = seq_avg * total_images

    for wid in sorted(warm_results):
        r = warm_results[wid]
        if "error" in r:
            print(f"    Worker {wid}: FAILED — {r['error']}")
        else:
            per_run = "  ".join(_fmt(t) for t in r["times"])
            print(f"    Worker {wid}: {per_run}  (avg {_fmt(r['avg'])})")

    print()
    print(f"    Wall clock ({total_images} images): {_fmt(t_wall_warm)}")
    print(f"    Sequential equiv:       {_fmt(seq_equivalent)}")
    if t_wall_warm > 0:
        speedup = seq_equivalent / t_wall_warm
        efficiency = speedup / n_workers * 100
        print(f"    Speedup:                {speedup:.2f}x")
        print(f"    Efficiency:             {efficiency:.0f}%")

    del loaded_models


# ── Main ─────────────────────────────────────────────────────


BENCHMARKS = [
    ("End-to-end pipeline", bench_end_to_end),
    ("Per-step breakdown", bench_per_step),
    ("Cellpose cold vs warm", bench_cold_warm),
    ("Device comparison", bench_device_comparison),
    ("Parallel segmentation", bench_parallel),
]


def main():
    parser = argparse.ArgumentParser(
        description="Performance benchmarks for rare_event_selection")
    parser.add_argument(
        "--source", default="skimage.human_mitosis",
        help="Data source: skimage.human_mitosis or path to image",
    )
    parser.add_argument(
        "--workers", type=int, default=3,
        help="Number of parallel workers (default: 3)",
    )
    args = parser.parse_args()

    import engine

    devices = _detect_devices()
    device_strs = [label for label, _ in devices]

    _banner("SMART Analysis — Rare Event Selection Benchmarks")
    print()
    print(f"  Engine:      {engine.__version__}")
    print(f"  Python:      {sys.version.split()[0]}")
    print(f"  Source:      {args.source}")
    print(f"  Devices:     {', '.join(device_strs)}")
    print(f"  Workers:     {args.workers}")

    # Pre-load image to validate
    try:
        img = _load_image(args.source)
        print(f"  Image:       {img.shape}, dtype={img.dtype}")
    except Exception as e:
        print(f"  ERROR: Could not load image: {e}")
        sys.exit(1)
    print()

    t_total = time.perf_counter()

    for name, func in BENCHMARKS:
        _section(name)
        try:
            func(args.source, n_workers=args.workers)
        except Exception as e:
            print(f"  ERROR: {e}")
        print()

    elapsed = time.perf_counter() - t_total
    _banner(f"Total benchmark time: {_fmt(elapsed)}")


if __name__ == "__main__":
    main()
