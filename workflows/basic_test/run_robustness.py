"""
Robustness tests for the v3 pipeline engine.

Tests edge cases, error recovery, resource cleanup, and unusual
usage patterns that go beyond the standard test suite:

   1. Metadata tampering         — step modifies metadata, next step works
   2. Identity passthrough       — step returns data unmodified
   3. Resource cleanup (normal)  — no leaked processes after shutdown
   4. Resource cleanup (error)   — no leaked processes after failure
   5. Duplicate scope_complete   — second call raises ScopeError
   6. Submit after scope_complete — new group accepted after completing one
   7. Concurrent scope_complete  — simultaneous scope_complete for different groups
   8. Large pipeline             — 10-step pipeline completes correctly
   9. Scope with single job      — degenerate case works
  10. Combined spatial+temporal  — both scope axes in one pipeline
  11. Empty string scope values  — spatial={"region": ""} works
  12. Scoped YAML pipeline       — real YAML pipelines with scope API
  13. Multi-run resource sharing — two runs share engine, both complete
  14. Repeated runs on same engine — sequential runs, no state leakage

Usage:
    python run_robustness.py
"""

import sys
import os
import time
import textwrap
import tempfile
import shutil
import atexit
import threading
import multiprocessing
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

WIDTH = 70
_TEMP = tempfile.mkdtemp(prefix="robust_")
atexit.register(shutil.rmtree, _TEMP, True)
_counter = 0


def _next_id():
    global _counter
    _counter += 1
    return _counter


def _temp_step(code, name=None):
    """Write a temporary step .py file to _TEMP."""
    path = Path(_TEMP) / (f"{name}.py" if name else f"step_{_next_id()}.py")
    path.write_text(textwrap.dedent(code))
    return str(path)


def _temp_yaml(content):
    """Write a temporary YAML pipeline file to _TEMP."""
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


# ── Tests ────────────────────────────────────────────────────


def test_metadata_tampering():
    """Step modifies metadata; next step still executes correctly."""
    from engine import run_pipeline

    base = Path(__file__).parent
    yaml_path = str(base / "pipelines" / "test_metadata_tamper_pipeline.yaml")

    r = run_pipeline(yaml_path, "tamper_test", {"original": True})

    tamper = r.get("step_metadata_tamper", {})
    local2 = r.get("step_local_2", {})

    if not tamper.get("executed"):
        return False, "step_metadata_tamper did not execute"
    if not local2.get("executed"):
        return False, "step_local_2 did not execute after metadata tamper"
    if r["metadata"].get("label") != "TAMPERED":
        return False, f"metadata label was not tampered: {r['metadata'].get('label')}"
    if r["metadata"].get("injected_key") != "injected_value":
        return False, "injected metadata key missing"

    return True, "both steps ran, metadata modifications persisted"


def test_identity_passthrough():
    """Identity step returns data unmodified; pipeline continues."""
    from engine import run_pipeline

    base = Path(__file__).parent
    yaml_path = str(base / "pipelines" / "test_identity_pipeline.yaml")

    r = run_pipeline(yaml_path, "identity_test", {"marker": "test_value"})

    local1 = r.get("step_local", {})
    local2 = r.get("step_local_2", {})

    if not local1.get("executed"):
        return False, "step_local did not execute"
    if not local2.get("executed"):
        return False, "step_local_2 did not execute after passthrough"
    if r.get("input", {}).get("marker") != "test_value":
        return False, "input data lost through passthrough"

    # step_local_2 should see step_local in previous steps
    prev = local2.get("previous_steps_found", [])
    if "step_local" not in prev:
        return False, f"step_local_2 didn't see step_local: {prev}"

    return True, "data flowed through identity step intact"


def test_resource_cleanup_normal():
    """No leaked child processes after normal engine shutdown."""
    _temp_step("def run(pd, **p): pd['ok'] = True; return pd", name="rc_ok")
    yaml = _temp_yaml("wf:\n  - rc_ok:")

    from engine import PipelineEngine

    children_before = _count_children()

    with PipelineEngine(max_concurrent=4) as e:
        run = e.create_run(yaml)
        futures = [run.submit(f"j{i}", {}) for i in range(8)]
        for f in futures:
            f.result(timeout=30)

    # Give OS a moment to reap processes
    time.sleep(0.2)
    children_after = _count_children()

    if children_after > children_before:
        return False, (f"leaked processes: {children_before} before, "
                       f"{children_after} after")

    return True, f"process count stable ({children_before} -> {children_after})"


def test_resource_cleanup_error():
    """No leaked child processes after a failed pipeline."""
    _temp_step('def run(pd, **p): raise RuntimeError("boom")', name="rc_err")
    yaml = _temp_yaml("wf:\n  - rc_err:")

    from engine import PipelineEngine

    children_before = _count_children()

    with PipelineEngine(max_concurrent=4) as e:
        run = e.create_run(yaml)
        futures = [run.submit(f"j{i}", {}) for i in range(4)]
        for f in futures:
            try:
                f.result(timeout=30)
            except Exception:
                pass

    time.sleep(0.2)
    children_after = _count_children()

    if children_after > children_before:
        return False, (f"leaked processes after error: {children_before} -> "
                       f"{children_after}")

    return True, f"process count stable after errors ({children_before} -> {children_after})"


def test_duplicate_scope_complete():
    """Second scope_complete for the same group raises ScopeError."""
    from engine import PipelineEngine, ScopeError

    _temp_step("""
        def run(pd, **p):
            pd["v"] = pd["input"]["v"]
            return pd
    """, name="dsc_a")
    _temp_step("""
        def run(pd, **p):
            pd["n"] = len(pd["results"])
            return pd
    """, name="dsc_b")
    yaml = _temp_yaml("""
        wf:
          - dsc_a:
          - dsc_b:
              scope:
                spatial: r
    """)

    with PipelineEngine() as e:
        run = e.create_run(yaml)
        run.submit("j1", {"v": 1}, spatial={"r": "X"})
        run.submit("j2", {"v": 2}, spatial={"r": "X"})

        # First scope_complete should succeed
        r = run.scope_complete(spatial={"r": "X"}).result(timeout=30)
        if r.get("n") != 2:
            return False, f"first scope_complete wrong: n={r.get('n')}"

        # Second scope_complete for same group should fail
        try:
            run.scope_complete(spatial={"r": "X"}).result(timeout=30)
            return False, "second scope_complete did not raise"
        except ScopeError:
            return True, "ScopeError raised on duplicate scope_complete"
        except Exception as exc:
            return False, f"wrong exception: {type(exc).__name__}: {exc}"


def test_submit_after_scope_complete():
    """New jobs can be submitted to a different scope group after completing one."""
    from engine import PipelineEngine

    _temp_step("""
        def run(pd, **p):
            pd["v"] = pd["input"]["v"]
            return pd
    """, name="sasc_a")
    _temp_step("""
        def run(pd, **p):
            pd["total"] = sum(r["v"] for r in pd["results"])
            return pd
    """, name="sasc_b")
    yaml = _temp_yaml("""
        wf:
          - sasc_a:
          - sasc_b:
              scope:
                spatial: r
    """)

    with PipelineEngine() as e:
        run = e.create_run(yaml)

        # Complete group A
        run.submit("a1", {"v": 10}, spatial={"r": "A"})
        ra = run.scope_complete(spatial={"r": "A"}).result(timeout=30)

        # Submit and complete group B after A is done
        run.submit("b1", {"v": 20}, spatial={"r": "B"})
        run.submit("b2", {"v": 30}, spatial={"r": "B"})
        rb = run.scope_complete(spatial={"r": "B"}).result(timeout=30)

    if ra.get("total") != 10:
        return False, f"group A total wrong: {ra.get('total')}"
    if rb.get("total") != 50:
        return False, f"group B total wrong: {rb.get('total')}"

    return True, f"group A={ra['total']}, group B={rb['total']}"


def test_concurrent_scope_complete():
    """Multiple scope_complete calls for different groups simultaneously."""
    from engine import PipelineEngine

    _temp_step("""
        def run(pd, **p):
            pd["v"] = pd["input"]["v"]
            return pd
    """, name="csc_a")
    _temp_step("""
        def run(pd, **p):
            pd["values"] = sorted(r["v"] for r in pd["results"])
            return pd
    """, name="csc_b")
    yaml = _temp_yaml("""
        wf:
          - csc_a:
          - csc_b:
              scope:
                spatial: r
    """)

    with PipelineEngine(max_concurrent=8) as e:
        run = e.create_run(yaml)

        # Submit 3 groups with 3 jobs each
        for group in ["X", "Y", "Z"]:
            for i in range(3):
                v = {"X": 100, "Y": 200, "Z": 300}[group] + i
                run.submit(f"{group}_{i}", {"v": v}, spatial={"r": group})

        # Trigger all scope_complete calls concurrently
        futures = {
            g: run.scope_complete(spatial={"r": g})
            for g in ["X", "Y", "Z"]
        }

        results = {}
        for g, f in futures.items():
            results[g] = f.result(timeout=30)

    if results["X"]["values"] != [100, 101, 102]:
        return False, f"group X wrong: {results['X']['values']}"
    if results["Y"]["values"] != [200, 201, 202]:
        return False, f"group Y wrong: {results['Y']['values']}"
    if results["Z"]["values"] != [300, 301, 302]:
        return False, f"group Z wrong: {results['Z']['values']}"

    return True, "3 groups completed concurrently with correct results"


def test_large_pipeline():
    """10-step pipeline completes correctly with data flowing through all steps."""
    steps = []
    for i in range(10):
        name = f"lp_s{i}"
        _temp_step(f"""
            def run(pd, **p):
                pd["step_{i}"] = True
                pd["last_step"] = {i}
                return pd
        """, name=name)
        steps.append(name)

    step_lines = "\n".join(f"          - {s}:" for s in steps)
    yaml = _temp_yaml(f"""
        wf:
{step_lines}
    """)

    from engine import run_pipeline
    r = run_pipeline(yaml, "large", {})

    for i in range(10):
        if not r.get(f"step_{i}"):
            return False, f"step_{i} did not execute"

    if r.get("last_step") != 9:
        return False, f"last_step was {r.get('last_step')}, expected 9"

    return True, f"all 10 steps executed, last_step=9"


def test_scope_single_job():
    """Scope with a single submitted job (degenerate case)."""
    from engine import PipelineEngine

    _temp_step("""
        def run(pd, **p):
            pd["v"] = pd["input"]["v"]
            return pd
    """, name="ssj_a")
    _temp_step("""
        def run(pd, **p):
            pd["collected"] = [r["v"] for r in pd["results"]]
            return pd
    """, name="ssj_b")
    yaml = _temp_yaml("""
        wf:
          - ssj_a:
          - ssj_b:
              scope:
                spatial: r
    """)

    with PipelineEngine() as e:
        run = e.create_run(yaml)
        run.submit("only_one", {"v": 42}, spatial={"r": "solo"})
        r = run.scope_complete(spatial={"r": "solo"}).result(timeout=30)

    if r.get("collected") != [42]:
        return False, f"collected={r.get('collected')}, expected [42]"

    return True, "single job scoped correctly"


def test_combined_spatial_temporal():
    """Jobs with both spatial and temporal scope labels."""
    from engine import PipelineEngine

    _temp_step("""
        def run(pd, **p):
            pd["v"] = pd["input"]["v"]
            return pd
    """, name="cst_a")
    _temp_step("""
        def run(pd, **p):
            pd["values"] = sorted(r["v"] for r in pd["results"])
            return pd
    """, name="cst_b")
    yaml = _temp_yaml("""
        wf:
          - cst_a:
          - cst_b:
              scope:
                spatial: region
    """)

    with PipelineEngine() as e:
        run = e.create_run(yaml)
        # Same spatial region, same temporal label
        run.submit("j1", {"v": 1},
                   spatial={"region": "R1"}, temporal={"t": "t0"})
        run.submit("j2", {"v": 2},
                   spatial={"region": "R1"}, temporal={"t": "t0"})
        # Different spatial region, same temporal
        run.submit("j3", {"v": 10},
                   spatial={"region": "R2"}, temporal={"t": "t0"})

        r1 = run.scope_complete(
            spatial={"region": "R1"}, temporal={"t": "t0"}
        ).result(timeout=30)
        r2 = run.scope_complete(
            spatial={"region": "R2"}, temporal={"t": "t0"}
        ).result(timeout=30)

    if r1["values"] != [1, 2]:
        return False, f"R1 values wrong: {r1['values']}"
    if r2["values"] != [10]:
        return False, f"R2 values wrong: {r2['values']}"

    return True, f"R1={r1['values']}, R2={r2['values']}"


def test_empty_string_scope_values():
    """Scope with empty string values works correctly."""
    from engine import PipelineEngine

    _temp_step("""
        def run(pd, **p):
            pd["v"] = pd["input"]["v"]
            return pd
    """, name="essv_a")
    _temp_step("""
        def run(pd, **p):
            pd["n"] = len(pd["results"])
            return pd
    """, name="essv_b")
    yaml = _temp_yaml("""
        wf:
          - essv_a:
          - essv_b:
              scope:
                spatial: region
    """)

    with PipelineEngine() as e:
        run = e.create_run(yaml)
        run.submit("j1", {"v": 1}, spatial={"region": ""})
        run.submit("j2", {"v": 2}, spatial={"region": ""})
        r = run.scope_complete(spatial={"region": ""}).result(timeout=30)

    if r.get("n") != 2:
        return False, f"expected 2 results, got {r.get('n')}"

    return True, f"empty string scope: n={r['n']}"


def test_scoped_yaml_spatial():
    """Run the scoped spatial YAML pipeline with real scope API."""
    from engine import PipelineEngine

    base = Path(__file__).parent
    yaml_path = str(base / "pipelines" / "test_scoped_spatial_pipeline.yaml")

    with PipelineEngine() as e:
        run = e.create_run(yaml_path)

        # Submit a 2x2 tile grid
        for row in range(2):
            for col in range(2):
                run.submit(
                    f"tile_{row}_{col}",
                    {"row": row, "col": col},
                    spatial={"region": "grid_A"},
                )

        r = run.scope_complete(
            spatial={"region": "grid_A"}
        ).result(timeout=30)

    stitched = r.get("stitched", {})
    if stitched.get("n_tiles") != 4:
        return False, f"expected 4 tiles, got {stitched.get('n_tiles')}"
    if stitched.get("n_rows") != 2:
        return False, f"expected 2 rows, got {stitched.get('n_rows')}"
    if stitched.get("n_cols") != 2:
        return False, f"expected 2 cols, got {stitched.get('n_cols')}"

    return True, (f"4 tiles stitched: {stitched['n_rows']}x{stitched['n_cols']} grid, "
                  f"total_value={stitched['total_value']}")


def test_scoped_yaml_multi_step():
    """Run the multi-step scoped YAML pipeline."""
    from engine import PipelineEngine

    base = Path(__file__).parent
    yaml_path = str(base / "pipelines" / "test_scoped_multi_step_pipeline.yaml")

    with PipelineEngine() as e:
        run = e.create_run(yaml_path)

        for row in range(2):
            run.submit(
                f"tile_{row}",
                {"row": row, "col": 0},
                spatial={"region": "strip"},
            )

        r = run.scope_complete(
            spatial={"region": "strip"}
        ).result(timeout=30)

    stitched = r.get("stitched", {})
    if stitched.get("n_tiles") != 2:
        return False, f"expected 2 tiles, got {stitched.get('n_tiles')}"

    # Each Phase 0 result should have step_local data too
    tiles = stitched.get("tiles", [])
    if not tiles:
        return False, "no tile data in stitched result"

    return True, f"multi-step: {stitched['n_tiles']} tiles stitched"


def test_multi_run_resource_sharing():
    """Two runs sharing one engine both complete correctly."""
    _temp_step("""
        import time
        def run(pd, **p):
            time.sleep(0.02)
            pd["run_id"] = pd["input"]["run_id"]
            pd["job_id"] = pd["input"]["job_id"]
            return pd
    """, name="mrrs")
    yaml = _temp_yaml("wf:\n  - mrrs:")

    from engine import PipelineEngine

    with PipelineEngine(max_concurrent=8) as e:
        run_a = e.create_run(yaml, priority="high")
        run_b = e.create_run(yaml)

        futures_a = [
            run_a.submit(f"a{i}", {"run_id": "A", "job_id": i})
            for i in range(5)
        ]
        futures_b = [
            run_b.submit(f"b{i}", {"run_id": "B", "job_id": i})
            for i in range(5)
        ]

        results_a = [f.result(timeout=30) for f in futures_a]
        results_b = [f.result(timeout=30) for f in futures_b]

    a_ids = sorted(r["run_id"] for r in results_a)
    b_ids = sorted(r["run_id"] for r in results_b)

    if a_ids != ["A"] * 5:
        return False, f"run A ids wrong: {a_ids}"
    if b_ids != ["B"] * 5:
        return False, f"run B ids wrong: {b_ids}"

    # Verify status after shutdown
    status = e.status()
    runs = status.get("runs", [])
    if len(runs) != 2:
        return False, f"expected 2 runs in status, got {len(runs)}"

    return True, "2 runs shared engine, 10 total jobs completed"


def test_repeated_runs_same_engine():
    """Sequential runs on the same engine with no state leakage."""
    _temp_step("""
        def run(pd, **p):
            pd["marker"] = pd["input"]["marker"]
            return pd
    """, name="rrse")
    yaml = _temp_yaml("wf:\n  - rrse:")

    from engine import PipelineEngine

    with PipelineEngine(max_concurrent=4) as e:
        # First run
        run1 = e.create_run(yaml)
        r1_futures = [
            run1.submit(f"r1_j{i}", {"marker": f"run1_{i}"})
            for i in range(3)
        ]
        r1_results = [f.result(timeout=30) for f in r1_futures]

        # Second run on the same engine
        run2 = e.create_run(yaml)
        r2_futures = [
            run2.submit(f"r2_j{i}", {"marker": f"run2_{i}"})
            for i in range(3)
        ]
        r2_results = [f.result(timeout=30) for f in r2_futures]

    # Check no cross-contamination
    r1_markers = sorted(r["marker"] for r in r1_results)
    r2_markers = sorted(r["marker"] for r in r2_results)

    expected_r1 = sorted(f"run1_{i}" for i in range(3))
    expected_r2 = sorted(f"run2_{i}" for i in range(3))

    if r1_markers != expected_r1:
        return False, f"run1 markers wrong: {r1_markers}"
    if r2_markers != expected_r2:
        return False, f"run2 markers wrong: {r2_markers}"

    # Verify engine tracked both runs
    status = e.status()
    if len(status.get("runs", [])) != 2:
        return False, f"expected 2 runs, got {len(status.get('runs', []))}"

    return True, "2 sequential runs, no state leakage"


# ── Helpers ──────────────────────────────────────────────────


def _count_children():
    """Count child processes of the current process."""
    current = multiprocessing.current_process()
    # Use multiprocessing's active_children which works cross-platform
    return len(multiprocessing.active_children())


# ── Runner ───────────────────────────────────────────────────


TESTS = [
    ("Metadata tampering",                test_metadata_tampering),
    ("Identity passthrough",              test_identity_passthrough),
    ("Resource cleanup (normal)",         test_resource_cleanup_normal),
    ("Resource cleanup (error)",          test_resource_cleanup_error),
    ("Duplicate scope_complete",          test_duplicate_scope_complete),
    ("Submit after scope_complete",       test_submit_after_scope_complete),
    ("Concurrent scope_complete",         test_concurrent_scope_complete),
    ("Large pipeline (10 steps)",         test_large_pipeline),
    ("Scope with single job",            test_scope_single_job),
    ("Combined spatial+temporal",         test_combined_spatial_temporal),
    ("Empty string scope values",         test_empty_string_scope_values),
    ("Scoped YAML spatial pipeline",      test_scoped_yaml_spatial),
    ("Scoped YAML multi-step pipeline",   test_scoped_yaml_multi_step),
    ("Multi-run resource sharing",        test_multi_run_resource_sharing),
    ("Repeated runs on same engine",      test_repeated_runs_same_engine),
]


def main():
    import engine

    print()
    print("=" * WIDTH)
    print("  SMART Analysis v3 -- Robustness Tests")
    print("=" * WIDTH)
    print()
    print(f"  Engine:   {engine.__version__}")
    print(f"  Python:   {sys.version.split()[0]} ({sys.executable})")
    print(f"  Temp:     {_TEMP}")
    print()

    t_total = time.perf_counter()
    results = []

    for i, (name, func) in enumerate(TESTS, 1):
        print("-" * WIDTH)
        print(f"  [{i:2d}/{len(TESTS)}] {name}")
        print("-" * WIDTH)

        t0 = time.perf_counter()
        try:
            passed, detail = func()
        except Exception as exc:
            passed = False
            detail = f"{type(exc).__name__}: {exc}"
        elapsed = time.perf_counter() - t0

        status = "PASS" if passed else "FAIL"
        print(f"  [{status}]  {detail}  ({_fmt(elapsed)})")
        print()

        results.append((name, passed, detail, elapsed))

    # ── Summary ──────────────────────────────────────────────

    elapsed_total = time.perf_counter() - t_total
    n_pass = sum(1 for _, p, _, _ in results if p)
    n_fail = sum(1 for _, p, _, _ in results if not p)

    print("=" * WIDTH)
    print("  Results")
    print("=" * WIDTH)
    print()

    for name, passed, detail, elapsed in results:
        icon = "[ OK ]" if passed else "[FAIL]"
        print(f"  {icon}  {name:<40s}  {_fmt(elapsed):>8s}")

    print()
    print(f"  {'_' * (WIDTH - 4)}")
    print(f"  Passed:  {n_pass}/{len(TESTS)}")
    print(f"  Failed:  {n_fail}/{len(TESTS)}")
    print(f"  Time:    {_fmt(elapsed_total)}")
    print()

    if n_fail == 0:
        print("  ALL ROBUSTNESS TESTS PASSED")
    else:
        print("  SOME TESTS FAILED")
        print()
        for name, passed, detail, _ in results:
            if not passed:
                print(f"    ** {name}: {detail}")

    print()
    print("=" * WIDTH)

    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
