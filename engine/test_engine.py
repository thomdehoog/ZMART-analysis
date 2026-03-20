"""
Test suite for the v3 pipeline engine.

Covers the full stack: module loading, AST-based METADATA extraction,
per-environment workers (oneshot and persistent), worker pool with GPU slot
and priority queue, phased pipeline execution, scope tracking with result
accumulation, multi-run interleaving, observability, backwards compatibility,
and regression guards.

Structure
---------
- TestErrors                  Exception hierarchy
- TestLoader                  AST-based METADATA extraction + exec loading
- TestPhases                  Phase splitting from YAML step lists
- TestWorkerProtocol          Per-environment workers, multi-step, caching
- TestWorkerErrorPaths        Crash, timeout, missing file
- TestPoolCPU                 Per-env workers, oneshot, reaper
- TestPoolGPU                 Mutual exclusion, priority, env switching
- TestRunSubmit               Job submission, immediate execution
- TestRunScopes               Spatial, temporal, combined scope tracking
- TestPipelineEngine          create_run, status, shutdown
- TestBackwardsCompat         run_pipeline convenience function
- TestReturnValidation        Step return type enforcement
- TestYAMLEdgeCases           Config parsing edge cases
- TestInputData               Input handling (None, falsy, etc.)
- TestLifecycle               Engine startup/shutdown
- TestMultiRun                Interleaved runs, different pipelines
- TestObservability           status() reporting
- TestRegression              Guards against specific past bugs
- TestPackageAPI              Public exports and versioning

Usage
-----
    python -m pytest engine/test_engine.py -v
    python -m pytest engine/test_engine.py -k Scopes -v
    python -m pytest engine/test_engine.py -v --tb=short
"""

import atexit
import os
import shutil
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

# Ensure the engine package is importable
ENGINE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ENGINE_DIR.parent))

from engine._loader import load_function, get_step_settings
from engine._run import split_phases, parse_yaml, _make_scope_key, StepConfig, Phase
from engine._errors import (
    WorkerError, WorkerSpawnError, WorkerCrashedError,
    StepExecutionError, ScopeError,
)

# Test fixtures
BASIC_TEST = ENGINE_DIR.parent / "workflows" / "basic_test"
STEPS_DIR = BASIC_TEST / "steps"
PIPELINES_DIR = BASIC_TEST / "pipelines"

# All temp files go here; cleaned up on exit
_TEMP_DIR = tempfile.mkdtemp(prefix="engine_test_")
atexit.register(shutil.rmtree, _TEMP_DIR, True)
_counter = 0


def _next_id():
    global _counter
    _counter += 1
    return _counter


def _temp_step(code, name=None):
    """Write a temporary step .py file to _TEMP_DIR."""
    path = Path(_TEMP_DIR) / (f"{name}.py" if name else
                               f"step_{_next_id()}.py")
    path.write_text(textwrap.dedent(code))
    return str(path)


def _temp_yaml(content):
    """Write a temporary YAML pipeline file to _TEMP_DIR."""
    text = textwrap.dedent(content)
    if "functions_dir" not in text:
        functions_dir = Path(_TEMP_DIR).as_posix()
        header = f'metadata:\n  functions_dir: "{functions_dir}"\n'
        if "metadata:" in text:
            text = text.replace("metadata:", header.rstrip("\n"), 1)
        else:
            text = header + text
    path = Path(_TEMP_DIR) / f"pipeline_{_next_id()}.yaml"
    path.write_text(text)
    return str(path)


# ── Errors ────────────────────────────────────────────────────


class TestErrors(unittest.TestCase):

    def test_worker_hierarchy(self):
        self.assertTrue(issubclass(WorkerSpawnError, WorkerError))
        self.assertTrue(issubclass(WorkerCrashedError, WorkerError))
        self.assertTrue(issubclass(StepExecutionError, WorkerError))

    def test_step_execution_error_stores_traceback(self):
        err = StepExecutionError("boom", remote_traceback="tb")
        self.assertEqual(str(err), "boom")
        self.assertEqual(err.remote_traceback, "tb")

    def test_step_execution_error_traceback_default_none(self):
        self.assertIsNone(StepExecutionError("x").remote_traceback)

    def test_scope_error_independent(self):
        """ScopeError is not a subclass of WorkerError."""
        self.assertFalse(issubclass(ScopeError, WorkerError))
        self.assertTrue(issubclass(ScopeError, Exception))


# ── Loader ────────────────────────────────────────────────────


class TestLoader(unittest.TestCase):

    def test_defaults_no_metadata(self):
        path = _temp_step("def run(pd, **p): return pd")
        s = get_step_settings(Path(path))
        self.assertEqual(s, {"environment": "local", "device": "cpu"})

    def test_explicit_environment_and_device(self):
        path = _temp_step("""
            METADATA = {"environment": "gpu_env", "device": "gpu"}
            def run(pd, **p): return pd
        """)
        s = get_step_settings(Path(path))
        self.assertEqual(s["environment"], "gpu_env")
        self.assertEqual(s["device"], "gpu")

    def test_device_defaults_to_cpu(self):
        path = _temp_step('METADATA = {"environment": "some_env"}')
        s = get_step_settings(Path(path))
        self.assertEqual(s["device"], "cpu")

    def test_no_worker_or_max_workers_in_output(self):
        """v3 does not expose worker/max_workers in step settings."""
        path = _temp_step('METADATA = {"worker": "persistent", "max_workers": 4}')
        s = get_step_settings(Path(path))
        self.assertNotIn("worker", s)
        self.assertNotIn("max_workers", s)

    def test_does_not_execute_module_code(self):
        path = _temp_step("""
            import nonexistent_package_xyz
            METADATA = {"environment": "safe", "device": "gpu"}
            def run(pd, **p): return pd
        """)
        s = get_step_settings(Path(path))
        self.assertEqual(s["environment"], "safe")
        self.assertEqual(s["device"], "gpu")

    def test_load_function_works(self):
        _temp_step("def run(pd, **p): pd['ok'] = True; return pd",
                   name="loader_test")
        module = load_function("loader_test", Path(_TEMP_DIR))
        result = module.run({})
        self.assertTrue(result["ok"])

    def test_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            load_function("does_not_exist", Path(_TEMP_DIR))

    def test_syntax_error(self):
        _temp_step("def run(\n    return {}", name="syn_err")
        with self.assertRaises(SyntaxError):
            load_function("syn_err", Path(_TEMP_DIR))


# ── Phases ────────────────────────────────────────────────────


class TestPhases(unittest.TestCase):

    def test_no_scope_single_phase(self):
        steps = [{"a": None}, {"b": {"x": 1}}]
        phases = split_phases(steps)
        self.assertEqual(len(phases), 1)
        self.assertIsNone(phases[0].scope)
        self.assertEqual(phases[0].steps[0].name, "a")
        self.assertEqual(phases[0].steps[1].params, {"x": 1})

    def test_one_scope_two_phases(self):
        steps = [
            {"preprocess": None},
            {"segment": None},
            {"stitch": {"scope": {"spatial": "region"}}},
            {"analyze": None},
        ]
        phases = split_phases(steps)
        self.assertEqual(len(phases), 2)
        self.assertIsNone(phases[0].scope)
        self.assertEqual([s.name for s in phases[0].steps],
                         ["preprocess", "segment"])
        self.assertEqual(phases[1].scope, {"spatial": "region"})
        self.assertEqual([s.name for s in phases[1].steps],
                         ["stitch", "analyze"])

    def test_two_scopes_three_phases(self):
        steps = [
            {"a": None},
            {"b": {"scope": {"spatial": "region"}}},
            {"c": None},
            {"d": {"scope": {"temporal": "session"}}},
        ]
        phases = split_phases(steps)
        self.assertEqual(len(phases), 3)
        self.assertIsNone(phases[0].scope)
        self.assertEqual(phases[1].scope, {"spatial": "region"})
        self.assertEqual(phases[2].scope, {"temporal": "session"})

    def test_scope_params_separated(self):
        """Scope is removed from params; other params preserved."""
        steps = [{"step": {"scope": {"spatial": "r"}, "sigma": 1.0}}]
        phases = split_phases(steps)
        self.assertEqual(phases[0].steps[0].params, {"sigma": 1.0})
        self.assertNotIn("scope", phases[0].steps[0].params)

    def test_scope_on_first_step(self):
        """First step having a scope means Phase 0 is empty (scope=that scope)."""
        steps = [{"a": {"scope": {"spatial": "r"}}}, {"b": None}]
        phases = split_phases(steps)
        self.assertEqual(len(phases), 1)
        self.assertEqual(phases[0].scope, {"spatial": "r"})

    def test_scope_key_creation(self):
        key = _make_scope_key({"region": "R3"}, {"timepoint": "t0"})
        self.assertEqual(key, (("spatial", "region", "R3"),
                                ("temporal", "timepoint", "t0")))

    def test_scope_key_empty(self):
        self.assertEqual(_make_scope_key({}, {}), ())


# ── Worker (protocol) ────────────────────────────────────────


class TestWorkerProtocol(unittest.TestCase):

    def test_execute_returns_result(self):
        from engine._worker import Worker
        path = _temp_step("""
            def run(pd, **p):
                pd["ran"] = True
                pd["x"] = p.get("x")
                return pd
        """)
        w = Worker("local", oneshot=True, connect_timeout=10)
        try:
            result = w.execute(path, {"input": 1}, {"x": 42}, timeout=10)
        finally:
            w.shutdown()
        self.assertTrue(result["ran"])
        self.assertEqual(result["x"], 42)
        self.assertEqual(result["input"], 1)

    def test_different_steps_same_worker(self):
        """A persistent worker can execute different step files."""
        from engine._worker import Worker
        path_a = _temp_step("""
            def run(pd, **p): pd["from"] = "a"; return pd
        """)
        path_b = _temp_step("""
            def run(pd, **p): pd["from"] = "b"; return pd
        """)
        w = Worker("local", oneshot=False, connect_timeout=10)
        try:
            ra = w.execute(path_a, {}, {}, timeout=10)
            rb = w.execute(path_b, {}, {}, timeout=10)
            self.assertEqual(ra["from"], "a")
            self.assertEqual(rb["from"], "b")
        finally:
            w.shutdown()

    def test_module_caching(self):
        """Same step file reuses cached module (load count stays at 1)."""
        from engine._worker import Worker
        path = _temp_step("""
            _n = 0
            def run(pd, **p):
                global _n
                _n += 1
                pd["n"] = _n
                return pd
        """)
        w = Worker("local", oneshot=False, connect_timeout=10)
        try:
            r1 = w.execute(path, {}, {}, timeout=10)
            r2 = w.execute(path, {}, {}, timeout=10)
            self.assertEqual(r1["n"], 1)
            self.assertEqual(r2["n"], 2)
        finally:
            w.shutdown()

    def test_persistent_reuses_process(self):
        from engine._worker import Worker
        path = _temp_step("""
            import os
            def run(pd, **p): pd["pid"] = os.getpid(); return pd
        """)
        w = Worker("local", oneshot=False, connect_timeout=10)
        try:
            r1 = w.execute(path, {}, {}, timeout=10)
            r2 = w.execute(path, {}, {}, timeout=10)
            self.assertEqual(r1["pid"], r2["pid"])
        finally:
            w.shutdown()

    def test_oneshot_exits(self):
        from engine._worker import Worker
        path = _temp_step("def run(pd, **p): return pd")
        w = Worker("local", oneshot=True, connect_timeout=10)
        w.execute(path, {}, {}, timeout=10)
        time.sleep(0.5)
        self.assertFalse(w.is_alive())
        w.shutdown()

    def test_complex_types(self):
        from engine._worker import Worker
        path = _temp_step("def run(pd, **p): return pd")
        data = {
            "tuple": (1, 2), "set": {3, 4}, "bytes": b"\xff",
            "nested": {"a": [None, True, {"b": 2.5}]},
        }
        w = Worker("local", oneshot=True, connect_timeout=10)
        try:
            r = w.execute(path, data, {}, timeout=10)
        finally:
            w.shutdown()
        self.assertEqual(r["tuple"], (1, 2))
        self.assertEqual(r["set"], {3, 4})
        self.assertEqual(r["nested"]["a"][2]["b"], 2.5)

    def test_shutdown_and_respawn(self):
        from engine._worker import Worker
        path = _temp_step("""
            import os
            def run(pd, **p): pd["pid"] = os.getpid(); return pd
        """)
        w = Worker("local", oneshot=False, connect_timeout=10)
        try:
            r1 = w.execute(path, {}, {}, timeout=10)
            w.shutdown()
            r2 = w.execute(path, {}, {}, timeout=10)
            self.assertNotEqual(r1["pid"], r2["pid"])
        finally:
            w.shutdown()

    def test_worker_status(self):
        from engine._worker import Worker
        path = _temp_step("def run(pd, **p): return pd")
        w = Worker("local", device="cpu", oneshot=True, connect_timeout=10)
        s = w.status
        self.assertEqual(s["state"], "stopped")
        w.execute(path, {}, {}, timeout=10)
        w.shutdown()


# ── Worker Error Paths ────────────────────────────────────────


class TestWorkerErrorPaths(unittest.TestCase):

    def test_crash_raises_worker_crashed(self):
        from engine._worker import Worker
        path = _temp_step("import os\ndef run(pd, **p): os._exit(1)")
        w = Worker("local", oneshot=True, connect_timeout=10)
        with self.assertRaises(WorkerCrashedError):
            w.execute(path, {}, {}, timeout=10)
        w.shutdown()

    def test_timeout_raises_step_execution_error(self):
        from engine._worker import Worker
        path = _temp_step("""
            import time
            def run(pd, **p): time.sleep(30); return pd
        """)
        w = Worker("local", oneshot=True, connect_timeout=10)
        with self.assertRaises(StepExecutionError) as ctx:
            w.execute(path, {}, {}, timeout=1)
        self.assertIn("timed out", str(ctx.exception))
        w.shutdown()

    def test_step_error_has_traceback(self):
        from engine._worker import Worker
        path = _temp_step('def run(pd, **p): raise ValueError("test")')
        w = Worker("local", oneshot=True, connect_timeout=10)
        with self.assertRaises(StepExecutionError) as ctx:
            w.execute(path, {}, {}, timeout=10)
        self.assertIn("test", str(ctx.exception))
        self.assertIn("ValueError", ctx.exception.remote_traceback)
        w.shutdown()


# ── Pool (CPU) ────────────────────────────────────────────────


class TestPoolCPU(unittest.TestCase):

    def test_per_env_worker_reuse(self):
        from engine._pool import WorkerPool
        path = _temp_step("""
            import os
            def run(pd, **p): pd["pid"] = os.getpid(); return pd
        """)
        pool = WorkerPool(idle_timeout=60)
        r1 = pool.execute("local", "cpu", path, {}, {}, timeout=10)
        r2 = pool.execute("local", "cpu", path, {}, {}, timeout=10)
        self.assertEqual(r1["pid"], r2["pid"])
        pool.shutdown_all()

    def test_oneshot_under_maximal(self):
        from engine._pool import WorkerPool
        path = _temp_step("""
            import os
            def run(pd, **p): pd["pid"] = os.getpid(); return pd
        """)
        pool = WorkerPool()
        r1 = pool.execute("local", "cpu", path, {}, {},
                           timeout=10, isolation="maximal")
        r2 = pool.execute("local", "cpu", path, {}, {},
                           timeout=10, isolation="maximal")
        self.assertNotEqual(r1["pid"], r2["pid"])
        pool.shutdown_all()

    def test_shutdown_before_use(self):
        from engine._pool import WorkerPool
        pool = WorkerPool()
        pool.shutdown_all()

    def test_error_through_pool(self):
        from engine._pool import WorkerPool
        path = _temp_step('def run(pd, **p): raise ValueError("pool err")')
        pool = WorkerPool()
        with self.assertRaises(StepExecutionError) as ctx:
            pool.execute("local", "cpu", path, {}, {}, timeout=10)
        self.assertIn("pool err", str(ctx.exception))
        pool.shutdown_all()

    def test_reaper_removes_idle(self):
        from engine._pool import WorkerPool
        path = _temp_step("def run(pd, **p): return pd")
        pool = WorkerPool(idle_timeout=0.2)
        pool.execute("local", "cpu", path, {}, {}, timeout=10)
        self.assertEqual(len([w for w in pool._cpu_workers.values()
                              if w.is_alive()]), 1)
        time.sleep(0.4)
        pool._reap_idle()
        self.assertEqual(len([w for w in pool._cpu_workers.values()
                              if w.is_alive()]), 0)
        pool.shutdown_all()


# ── Pool (GPU) ────────────────────────────────────────────────


class TestPoolGPU(unittest.TestCase):

    def test_gpu_mutual_exclusion(self):
        """Two GPU calls execute sequentially (one at a time)."""
        from engine._pool import WorkerPool
        path = _temp_step("""
            import time
            def run(pd, **p): time.sleep(0.3); pd["done"] = True; return pd
        """)
        pool = WorkerPool()
        results = []

        def run_one():
            r = pool.execute("local", "gpu", path, {}, {}, timeout=15)
            results.append(r)

        t0 = time.monotonic()
        threads = [threading.Thread(target=run_one) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        elapsed = time.monotonic() - t0
        pool.shutdown_all()

        self.assertEqual(len(results), 2)
        self.assertGreater(elapsed, 0.5, "GPU calls ran in parallel")

    def test_gpu_priority_ordering(self):
        """Higher priority GPU work executes before lower priority."""
        from engine._pool import WorkerPool
        path = _temp_step("""
            import time
            def run(pd, **p):
                time.sleep(0.1)
                pd["priority"] = p.get("p")
                return pd
        """)
        pool = WorkerPool()
        order = []
        lock = threading.Lock()

        def submit(priority):
            r = pool.execute("local", "gpu", path, {}, {"p": priority},
                             priority=priority, timeout=15)
            with lock:
                order.append(r["priority"])

        # Submit low priority first, then high — high should execute first
        # (after the GPU thread starts processing)
        threads = []
        for p in [0, 0, 10, 10]:
            t = threading.Thread(target=submit, args=(p,))
            threads.append(t)
        for t in threads:
            t.start()
            time.sleep(0.02)  # stagger submissions slightly
        for t in threads:
            t.join(timeout=30)
        pool.shutdown_all()

        self.assertEqual(len(order), 4)
        # First item is whatever got in first; subsequent items should
        # favor priority=10 over priority=0
        high_indices = [i for i, p in enumerate(order) if p == 10]
        low_indices = [i for i, p in enumerate(order) if p == 0]
        if high_indices and low_indices:
            self.assertLess(min(high_indices), max(low_indices),
                            f"High priority should run before low: {order}")

    def test_gpu_shutdown_drains_queue(self):
        from engine._pool import WorkerPool
        import concurrent.futures
        pool = WorkerPool()
        # Don't start any work, just put something in queue
        future = concurrent.futures.Future()
        pool._gpu_queue.put((0, 0, "local", "/fake", {}, {}, 10, "minimal", future))
        pool.shutdown_all()
        self.assertTrue(future.done())
        with self.assertRaises(WorkerError):
            future.result()


# ── Run (submit) ──────────────────────────────────────────────


class TestRunSubmit(unittest.TestCase):

    def test_simple_submit(self):
        _temp_step("def run(pd, **p): pd['ok'] = True; return pd",
                   name="sub_a")
        yaml = _temp_yaml("wf:\n  - sub_a:")
        from engine import PipelineEngine
        with PipelineEngine() as e:
            run = e.create_run(yaml)
            future = run.submit("t", {})
            r = future.result(timeout=10)
        self.assertTrue(r["ok"])

    def test_multi_step(self):
        _temp_step("def run(pd, **p): pd['s1'] = 1; return pd",
                   name="ms_a")
        _temp_step("def run(pd, **p): pd['s2'] = pd['s1'] + 1; return pd",
                   name="ms_b")
        yaml = _temp_yaml("wf:\n  - ms_a:\n  - ms_b:")
        from engine import PipelineEngine
        with PipelineEngine() as e:
            run = e.create_run(yaml)
            r = run.submit("t", {}).result(timeout=10)
        self.assertEqual(r["s1"], 1)
        self.assertEqual(r["s2"], 2)

    def test_data_flows(self):
        _temp_step("def run(pd, **p): pd['from_a'] = 'hello'; return pd",
                   name="df_a")
        _temp_step("""
            def run(pd, **p):
                pd['saw'] = pd.get('from_a')
                return pd
        """, name="df_b")
        yaml = _temp_yaml("wf:\n  - df_a:\n  - df_b:")
        from engine import PipelineEngine
        with PipelineEngine() as e:
            r = e.create_run(yaml).submit("t", {}).result(timeout=10)
        self.assertEqual(r["saw"], "hello")

    def test_input_data(self):
        _temp_step("def run(pd, **p): return pd", name="inp")
        yaml = _temp_yaml("wf:\n  - inp:")
        from engine import PipelineEngine
        with PipelineEngine() as e:
            r = e.create_run(yaml).submit("t", {"key": "val"}).result(timeout=10)
        self.assertEqual(r["input"]["key"], "val")

    def test_concurrent_submits(self):
        _temp_step("""
            def run(pd, **p):
                pd["job"] = pd["input"]["job"]
                return pd
        """, name="conc")
        yaml = _temp_yaml("wf:\n  - conc:")
        from engine import PipelineEngine
        with PipelineEngine() as e:
            run = e.create_run(yaml)
            futures = [run.submit(f"j{i}", {"job": i}) for i in range(5)]
            results = [f.result(timeout=30) for f in futures]
        self.assertEqual(sorted(r["job"] for r in results), list(range(5)))


# ── Run (scopes) ─────────────────────────────────────────────


class TestRunScopes(unittest.TestCase):

    def test_spatial_scope(self):
        """Scoped step receives accumulated results from all jobs."""
        _temp_step("""
            def run(pd, **p):
                pd["tile"] = pd["input"]["tile"]
                return pd
        """, name="sc_seg")
        _temp_step("""
            def run(pd, **p):
                tiles = [r["tile"] for r in pd["results"]]
                pd["tiles"] = sorted(tiles)
                return pd
        """, name="sc_stitch")
        yaml = _temp_yaml("""
            wf:
              - sc_seg:
              - sc_stitch:
                  scope:
                    spatial: region
        """)
        from engine import PipelineEngine
        with PipelineEngine() as e:
            run = e.create_run(yaml)
            for i in range(3):
                run.submit(f"tile_{i}", {"tile": i},
                           spatial={"region": "R1"},
                           temporal={"t": "0"})
            future = run.scope_complete(
                spatial={"region": "R1"}, temporal={"t": "0"})
            r = future.result(timeout=30)
        self.assertEqual(r["tiles"], [0, 1, 2])

    def test_scoped_step_metadata(self):
        """Scoped step receives metadata with phase and n_accumulated."""
        _temp_step("def run(pd, **p): return pd", name="sc_m1")
        _temp_step("def run(pd, **p): return pd", name="sc_m2")
        yaml = _temp_yaml("""
            wf:
              - sc_m1:
              - sc_m2:
                  scope:
                    spatial: r
        """)
        from engine import PipelineEngine
        with PipelineEngine() as e:
            run = e.create_run(yaml)
            run.submit("j1", {}, spatial={"r": "1"})
            run.submit("j2", {}, spatial={"r": "1"})
            r = run.scope_complete(spatial={"r": "1"}).result(timeout=30)
        self.assertEqual(r["metadata"]["n_accumulated"], 2)
        self.assertEqual(r["metadata"]["phase"], 1)

    def test_steps_after_scope(self):
        """Steps after a scoped step process the single scoped result."""
        _temp_step("def run(pd, **p): pd['v'] = pd['input']['v']; return pd",
                   name="sc_pre")
        _temp_step("""
            def run(pd, **p):
                pd["sum"] = sum(r["v"] for r in pd["results"])
                return pd
        """, name="sc_agg")
        _temp_step("""
            def run(pd, **p):
                pd["final"] = pd["sum"] * 2
                return pd
        """, name="sc_post")
        yaml = _temp_yaml("""
            wf:
              - sc_pre:
              - sc_agg:
                  scope:
                    spatial: r
              - sc_post:
        """)
        from engine import PipelineEngine
        with PipelineEngine() as e:
            run = e.create_run(yaml)
            for i in range(3):
                run.submit(f"j{i}", {"v": i + 1}, spatial={"r": "1"})
            r = run.scope_complete(spatial={"r": "1"}).result(timeout=30)
        self.assertEqual(r["sum"], 6)
        self.assertEqual(r["final"], 12)

    def test_scope_complete_no_jobs_raises(self):
        _temp_step("def run(pd, **p): return pd", name="sc_nj1")
        _temp_step("def run(pd, **p): return pd", name="sc_nj2")
        yaml = _temp_yaml("""
            wf:
              - sc_nj1:
              - sc_nj2:
                  scope:
                    spatial: r
        """)
        from engine import PipelineEngine
        with PipelineEngine() as e:
            run = e.create_run(yaml)
            with self.assertRaises(ScopeError):
                run.scope_complete(spatial={"r": "1"}).result(timeout=10)

    def test_scope_complete_wrong_axis_raises(self):
        _temp_step("def run(pd, **p): return pd", name="sc_wa1")
        _temp_step("def run(pd, **p): return pd", name="sc_wa2")
        yaml = _temp_yaml("""
            wf:
              - sc_wa1:
              - sc_wa2:
                  scope:
                    spatial: r
        """)
        from engine import PipelineEngine
        with PipelineEngine() as e:
            run = e.create_run(yaml)
            run.submit("j1", {}, spatial={"r": "1"})
            with self.assertRaises(ScopeError):
                # Wrong axis: temporal instead of spatial
                run.scope_complete(temporal={"t": "1"}).result(timeout=10)

    def test_multiple_scope_groups(self):
        """Different scope groups accumulate independently."""
        _temp_step("""
            def run(pd, **p):
                pd["v"] = pd["input"]["v"]
                return pd
        """, name="sc_mg1")
        _temp_step("""
            def run(pd, **p):
                pd["values"] = sorted(r["v"] for r in pd["results"])
                return pd
        """, name="sc_mg2")
        yaml = _temp_yaml("""
            wf:
              - sc_mg1:
              - sc_mg2:
                  scope:
                    spatial: r
        """)
        from engine import PipelineEngine
        with PipelineEngine() as e:
            run = e.create_run(yaml)
            # Group A
            run.submit("a1", {"v": 10}, spatial={"r": "A"})
            run.submit("a2", {"v": 20}, spatial={"r": "A"})
            # Group B
            run.submit("b1", {"v": 100}, spatial={"r": "B"})
            run.submit("b2", {"v": 200}, spatial={"r": "B"})

            ra = run.scope_complete(spatial={"r": "A"}).result(timeout=30)
            rb = run.scope_complete(spatial={"r": "B"}).result(timeout=30)

        self.assertEqual(ra["values"], [10, 20])
        self.assertEqual(rb["values"], [100, 200])


# ── Pipeline Engine ───────────────────────────────────────────


class TestPipelineEngine(unittest.TestCase):

    def test_create_run(self):
        _temp_step("def run(pd, **p): return pd", name="cr")
        yaml = _temp_yaml("wf:\n  - cr:")
        from engine import PipelineEngine
        with PipelineEngine() as e:
            run = e.create_run(yaml)
            self.assertIsNotNone(run)

    def test_create_run_with_string_priority(self):
        _temp_step("def run(pd, **p): return pd", name="crp")
        yaml = _temp_yaml("wf:\n  - crp:")
        from engine import PipelineEngine
        with PipelineEngine() as e:
            run = e.create_run(yaml, priority="high")
            self.assertEqual(run._priority, 10)

    def test_create_run_after_shutdown_raises(self):
        _temp_step("def run(pd, **p): return pd", name="crs")
        yaml = _temp_yaml("wf:\n  - crs:")
        from engine import PipelineEngine
        e = PipelineEngine()
        e.shutdown()
        with self.assertRaises(RuntimeError):
            e.create_run(yaml)

    def test_context_manager(self):
        from engine import PipelineEngine
        with PipelineEngine() as e:
            self.assertTrue(e._accepting)
        self.assertFalse(e._accepting)


# ── Backwards Compatibility ───────────────────────────────────


class TestBackwardsCompat(unittest.TestCase):

    def test_run_pipeline_simple(self):
        _temp_step("def run(pd, **p): pd['ok'] = True; return pd",
                   name="bc_s")
        yaml = _temp_yaml("wf:\n  - bc_s:")
        from engine import run_pipeline
        r = run_pipeline(yaml, "test", {})
        self.assertTrue(r["ok"])
        self.assertEqual(r["metadata"]["label"], "test")

    def test_run_pipeline_with_input(self):
        _temp_step("def run(pd, **p): return pd", name="bc_i")
        yaml = _temp_yaml("wf:\n  - bc_i:")
        from engine import run_pipeline
        r = run_pipeline(yaml, "test", {"key": "val"})
        self.assertEqual(r["input"]["key"], "val")

    def test_run_pipeline_error(self):
        _temp_step('def run(pd, **p): raise RuntimeError("fail")',
                   name="bc_e")
        yaml = _temp_yaml("wf:\n  - bc_e:")
        from engine import run_pipeline
        with self.assertRaises(RuntimeError):
            run_pipeline(yaml, "test", {})

    def test_run_pipeline_multi_step(self):
        _temp_step("def run(pd, **p): pd['a'] = 1; return pd", name="bc_m1")
        _temp_step("def run(pd, **p): pd['b'] = pd['a'] + 1; return pd",
                   name="bc_m2")
        yaml = _temp_yaml("wf:\n  - bc_m1:\n  - bc_m2:")
        from engine import run_pipeline
        r = run_pipeline(yaml, "test", {})
        self.assertEqual(r["b"], 2)


# ── Return Validation ─────────────────────────────────────────


class TestReturnValidation(unittest.TestCase):

    def test_none_return_raises(self):
        _temp_step("def run(pd, **p): return None", name="rv_none")
        yaml = _temp_yaml("wf:\n  - rv_none:")
        from engine import run_pipeline
        with self.assertRaises(TypeError) as ctx:
            run_pipeline(yaml, "t", {})
        self.assertIn("NoneType", str(ctx.exception))

    def test_list_return_raises(self):
        _temp_step("def run(pd, **p): return [1, 2]", name="rv_list")
        yaml = _temp_yaml("wf:\n  - rv_list:")
        from engine import run_pipeline
        with self.assertRaises(TypeError) as ctx:
            run_pipeline(yaml, "t", {})
        self.assertIn("list", str(ctx.exception))


# ── YAML Edge Cases ───────────────────────────────────────────


class TestYAMLEdgeCases(unittest.TestCase):

    def test_empty_steps_raises(self):
        yaml = _temp_yaml("wf:")
        from engine import run_pipeline
        with self.assertRaises(ValueError):
            run_pipeline(yaml, "t", {})

    def test_no_workflow_raises(self):
        yaml = _temp_yaml("metadata:\n  verbose: 0")
        from engine import run_pipeline
        with self.assertRaises(ValueError):
            run_pipeline(yaml, "t", {})

    def test_null_params(self):
        _temp_step("def run(pd, **p): pd['p'] = p; return pd",
                   name="yec_null")
        yaml = _temp_yaml("wf:\n  - yec_null:")
        from engine import run_pipeline
        r = run_pipeline(yaml, "t", {})
        self.assertEqual(r["p"], {})

    def test_nonexistent_yaml(self):
        from engine import run_pipeline
        with self.assertRaises(FileNotFoundError):
            run_pipeline("nonexistent.yaml", "t", {})

    def test_missing_step_raises(self):
        yaml = _temp_yaml("wf:\n  - does_not_exist:")
        from engine import run_pipeline
        with self.assertRaises(FileNotFoundError):
            run_pipeline(yaml, "t", {})


# ── Input Data ────────────────────────────────────────────────


class TestInputData(unittest.TestCase):

    def test_none_becomes_empty_dict(self):
        _temp_step("def run(pd, **p): return pd", name="id_n")
        yaml = _temp_yaml("wf:\n  - id_n:")
        from engine import run_pipeline
        r = run_pipeline(yaml, "t", None)
        self.assertEqual(r["input"], {})

    def test_falsy_values_preserved(self):
        _temp_step("def run(pd, **p): return pd", name="id_f")
        yaml = _temp_yaml("wf:\n  - id_f:")
        from engine import run_pipeline
        for val in ([], 0, False):
            r = run_pipeline(yaml, "t", val)
            self.assertEqual(r["input"], val, f"Failed for {val!r}")


# ── Lifecycle ─────────────────────────────────────────────────


class TestLifecycle(unittest.TestCase):

    def test_double_shutdown(self):
        from engine import PipelineEngine
        e = PipelineEngine()
        e.shutdown()
        e.shutdown()

    def test_repr(self):
        from engine import PipelineEngine
        with PipelineEngine() as e:
            self.assertIn("PipelineEngine", repr(e))


# ── Multi-Run ─────────────────────────────────────────────────


class TestMultiRun(unittest.TestCase):

    def test_two_runs_same_engine(self):
        _temp_step("def run(pd, **p): pd['t'] = 'A'; return pd",
                   name="mr_a")
        _temp_step("def run(pd, **p): pd['t'] = 'B'; return pd",
                   name="mr_b")
        yaml_a = _temp_yaml("wf:\n  - mr_a:")
        yaml_b = _temp_yaml("wf:\n  - mr_b:")
        from engine import PipelineEngine
        with PipelineEngine() as e:
            ra = e.create_run(yaml_a).submit("a", {}).result(timeout=10)
            rb = e.create_run(yaml_b).submit("b", {}).result(timeout=10)
        self.assertEqual(ra["t"], "A")
        self.assertEqual(rb["t"], "B")

    def test_concurrent_different_runs(self):
        _temp_step("""
            def run(pd, **p):
                pd["job"] = pd["input"]["job"]
                return pd
        """, name="mr_c")
        yaml = _temp_yaml("wf:\n  - mr_c:")
        from engine import PipelineEngine
        with PipelineEngine() as e:
            run_a = e.create_run(yaml, priority="high")
            run_b = e.create_run(yaml)
            fa = run_a.submit("a", {"job": "A"})
            fb = run_b.submit("b", {"job": "B"})
            ra = fa.result(timeout=30)
            rb = fb.result(timeout=30)
        self.assertEqual(ra["job"], "A")
        self.assertEqual(rb["job"], "B")

    def test_many_concurrent_jobs(self):
        _temp_step("""
            def run(pd, **p):
                pd["id"] = pd["input"]["id"]
                return pd
        """, name="mr_m")
        yaml = _temp_yaml("wf:\n  - mr_m:")
        from engine import PipelineEngine
        n = 20
        with PipelineEngine(max_concurrent=n) as e:
            run = e.create_run(yaml)
            futures = [run.submit(f"j{i}", {"id": i}) for i in range(n)]
            results = [f.result(timeout=30) for f in futures]
        self.assertEqual(sorted(r["id"] for r in results), list(range(n)))


# ── Observability ─────────────────────────────────────────────


class TestObservability(unittest.TestCase):

    def test_engine_status_structure(self):
        _temp_step("def run(pd, **p): return pd", name="obs")
        yaml = _temp_yaml("wf:\n  - obs:")
        from engine import PipelineEngine
        with PipelineEngine() as e:
            run = e.create_run(yaml)
            run.submit("j", {}).result(timeout=10)
            s = e.status()
        self.assertIn("workers", s)
        self.assertIn("gpu_queue_depth", s)
        self.assertIn("runs", s)
        self.assertEqual(len(s["runs"]), 1)

    def test_run_status_structure(self):
        _temp_step("def run(pd, **p): return pd", name="obs_r")
        yaml = _temp_yaml("wf:\n  - obs_r:")
        from engine import PipelineEngine
        with PipelineEngine() as e:
            run = e.create_run(yaml, priority="high")
            run.submit("j", {}).result(timeout=10)
            s = run.status
        self.assertEqual(s["priority"], 10)
        self.assertIn("completed", s)
        self.assertIn("phases", s)


# ── Integration: full pipeline workflows ──────────────────────


class TestIntegrationSimple(unittest.TestCase):
    """End-to-end tests for simple (no-scope) pipelines."""

    def test_four_step_pipeline(self):
        """Full pipeline: preprocess → transform → filter → output."""
        _temp_step("""
            def run(pd, **p):
                pd["preprocess"] = [x * p.get("scale", 1)
                                    for x in pd["input"]["data"]]
                return pd
        """, name="int_pre")
        _temp_step("""
            def run(pd, **p):
                pd["transform"] = [x ** 2 for x in pd["preprocess"]]
                return pd
        """, name="int_trans")
        _temp_step("""
            def run(pd, **p):
                threshold = p.get("threshold", 10)
                pd["filter"] = [x for x in pd["transform"] if x > threshold]
                return pd
        """, name="int_filt")
        _temp_step("""
            def run(pd, **p):
                pd["output"] = {
                    "count": len(pd["filter"]),
                    "values": pd["filter"],
                }
                return pd
        """, name="int_out")
        yaml = _temp_yaml("""
            wf:
              - int_pre:
                  scale: 2
              - int_trans:
              - int_filt:
                  threshold: 10
              - int_out:
        """)
        from engine import run_pipeline
        r = run_pipeline(yaml, "test", {"data": [1, 2, 3, 4, 5]})
        # scale: [2,4,6,8,10], square: [4,16,36,64,100], filter>10: [16,36,64,100]
        self.assertEqual(r["output"]["count"], 4)
        self.assertEqual(r["output"]["values"], [16, 36, 64, 100])

    def test_sequential_adaptive_feedback(self):
        """Each job uses the previous job's result — the adaptive loop."""
        _temp_step("""
            def run(pd, **p):
                t = pd["input"]["timepoint"]
                feedback = pd["input"].get("feedback")
                pd["result"] = {
                    "timepoint": t,
                    "used_feedback": feedback is not None,
                    "feedback_value": feedback,
                }
                return pd
        """, name="int_adapt")
        yaml = _temp_yaml("wf:\n  - int_adapt:")

        from engine import PipelineEngine
        with PipelineEngine() as engine:
            run = engine.create_run(yaml)
            feedback = None
            for t in range(4):
                r = run.submit(
                    f"t{t}", {"timepoint": t, "feedback": feedback},
                ).result(timeout=10)
                feedback = r["result"]

        self.assertEqual(r["result"]["timepoint"], 3)
        self.assertTrue(r["result"]["used_feedback"])
        self.assertEqual(r["result"]["feedback_value"]["timepoint"], 2)

    def test_engine_recovers_after_failed_job(self):
        """A failed job doesn't prevent subsequent jobs from running."""
        _temp_step('def run(pd, **p): raise RuntimeError("bad")',
                   name="int_fail")
        _temp_step('def run(pd, **p): pd["ok"] = True; return pd',
                   name="int_good")
        bad_yaml = _temp_yaml("wf:\n  - int_fail:")
        good_yaml = _temp_yaml("wf:\n  - int_good:")

        from engine import PipelineEngine
        with PipelineEngine() as engine:
            with self.assertRaises(RuntimeError):
                engine.create_run(bad_yaml).submit("bad", {}).result(timeout=10)
            r = engine.create_run(good_yaml).submit("good", {}).result(timeout=10)
            self.assertTrue(r["ok"])

    def test_concurrent_mixed_success_and_failure(self):
        """Some jobs fail, others succeed; all futures resolve correctly."""
        _temp_step('def run(pd, **p): raise ValueError("fail")',
                   name="int_wf")
        _temp_step('def run(pd, **p): pd["ok"] = True; return pd',
                   name="int_wp")
        fail_yaml = _temp_yaml("wf:\n  - int_wf:")
        pass_yaml = _temp_yaml("wf:\n  - int_wp:")

        from engine import PipelineEngine
        with PipelineEngine() as engine:
            f_fail = engine.create_run(fail_yaml).submit("fail", {})
            f_pass = engine.create_run(pass_yaml).submit("pass", {})

            with self.assertRaises(ValueError):
                f_fail.result(timeout=30)
            self.assertTrue(f_pass.result(timeout=30)["ok"])

    def test_growing_data_across_sequential_jobs(self):
        """Accumulated results grow across an adaptive run."""
        _temp_step("""
            def run(pd, **p):
                t = pd["input"]["timepoint"]
                history = pd["input"].get("history", [])
                pd["result"] = {
                    "timepoint": t,
                    "cells_found": t * 10 + 5,
                    "history_length": len(history),
                }
                return pd
        """, name="int_grow")
        yaml = _temp_yaml("wf:\n  - int_grow:")

        from engine import PipelineEngine
        history = []
        with PipelineEngine() as engine:
            run = engine.create_run(yaml)
            for t in range(4):
                r = run.submit(
                    f"t{t}", {"timepoint": t, "history": history},
                ).result(timeout=10)
                history.append(r["result"])

        self.assertEqual(len(history), 4)
        self.assertEqual(history[0]["history_length"], 0)
        self.assertEqual(history[3]["history_length"], 3)

    def test_successive_batches(self):
        """Engine processes multiple waves of concurrent jobs."""
        _temp_step("""
            def run(pd, **p):
                pd["batch"] = pd["input"]["batch"]
                pd["idx"] = pd["input"]["idx"]
                return pd
        """, name="int_batch")
        yaml = _temp_yaml("wf:\n  - int_batch:")

        from engine import PipelineEngine
        with PipelineEngine() as engine:
            run = engine.create_run(yaml)
            for batch in range(3):
                futures = [
                    run.submit(f"b{batch}_j{i}",
                               {"batch": batch, "idx": i})
                    for i in range(4)
                ]
                results = [f.result(timeout=30) for f in futures]
                for i, r in enumerate(results):
                    self.assertEqual(r["batch"], batch)
                    self.assertEqual(r["idx"], i)


class TestIntegrationScoped(unittest.TestCase):
    """End-to-end tests for scoped pipelines (spatial/temporal)."""

    def test_full_scoped_workflow(self):
        """Complete: per-tile processing → spatial aggregation → post-processing."""
        _temp_step("""
            def run(pd, **p):
                pd["features"] = pd["input"]["value"] * 2
                return pd
        """, name="isc_extract")
        _temp_step("""
            def run(pd, **p):
                all_features = [r["features"] for r in pd["results"]]
                pd["aggregated"] = sum(all_features)
                return pd
        """, name="isc_agg")
        _temp_step("""
            def run(pd, **p):
                pd["normalized"] = pd["aggregated"] / p.get("divisor", 1)
                return pd
        """, name="isc_norm")
        yaml = _temp_yaml("""
            wf:
              - isc_extract:
              - isc_agg:
                  scope:
                    spatial: region
              - isc_norm:
                  divisor: 3
        """)

        from engine import PipelineEngine
        with PipelineEngine() as e:
            run = e.create_run(yaml)
            for v in [10, 20, 30]:
                run.submit(f"t_{v}", {"value": v},
                           spatial={"region": "R1"})
            r = run.scope_complete(spatial={"region": "R1"}).result(timeout=30)

        # features: 20, 40, 60 → sum: 120 → /3 = 40
        self.assertEqual(r["normalized"], 40.0)

    def test_multiple_regions_independent(self):
        """Different spatial groups produce independent results."""
        _temp_step("""
            def run(pd, **p):
                pd["v"] = pd["input"]["v"]
                return pd
        """, name="isc_r1")
        _temp_step("""
            def run(pd, **p):
                pd["total"] = sum(r["v"] for r in pd["results"])
                return pd
        """, name="isc_r2")
        yaml = _temp_yaml("""
            wf:
              - isc_r1:
              - isc_r2:
                  scope:
                    spatial: region
        """)

        from engine import PipelineEngine
        with PipelineEngine() as e:
            run = e.create_run(yaml)
            # Region A: 1 + 2 = 3
            run.submit("a1", {"v": 1}, spatial={"region": "A"})
            run.submit("a2", {"v": 2}, spatial={"region": "A"})
            # Region B: 100 + 200 = 300
            run.submit("b1", {"v": 100}, spatial={"region": "B"})
            run.submit("b2", {"v": 200}, spatial={"region": "B"})

            ra = run.scope_complete(spatial={"region": "A"}).result(timeout=30)
            rb = run.scope_complete(spatial={"region": "B"}).result(timeout=30)

        self.assertEqual(ra["total"], 3)
        self.assertEqual(rb["total"], 300)

    def test_scope_waits_for_slow_jobs(self):
        """scope_complete blocks until all jobs in the group finish."""
        _temp_step("""
            import time
            def run(pd, **p):
                time.sleep(p.get("delay", 0))
                pd["v"] = pd["input"]["v"]
                return pd
        """, name="isc_slow")
        _temp_step("""
            def run(pd, **p):
                pd["values"] = sorted(r["v"] for r in pd["results"])
                return pd
        """, name="isc_coll")
        yaml = _temp_yaml("""
            wf:
              - isc_slow:
              - isc_coll:
                  scope:
                    spatial: r
        """)

        from engine import PipelineEngine
        with PipelineEngine() as e:
            run = e.create_run(yaml)
            run.submit("fast", {"v": 1}, spatial={"r": "X"})
            run.submit("slow", {"v": 2}, spatial={"r": "X"})
            r = run.scope_complete(spatial={"r": "X"}).result(timeout=30)

        self.assertEqual(r["values"], [1, 2])

    def test_concurrent_scope_groups(self):
        """Multiple scope groups can be completed concurrently."""
        _temp_step("""
            def run(pd, **p):
                pd["v"] = pd["input"]["v"]
                return pd
        """, name="isc_cg1")
        _temp_step("""
            def run(pd, **p):
                pd["sum"] = sum(r["v"] for r in pd["results"])
                return pd
        """, name="isc_cg2")
        yaml = _temp_yaml("""
            wf:
              - isc_cg1:
              - isc_cg2:
                  scope:
                    spatial: r
        """)

        from engine import PipelineEngine
        with PipelineEngine() as e:
            run = e.create_run(yaml)
            for region in ["A", "B", "C"]:
                for i in range(3):
                    run.submit(f"{region}_{i}", {"v": ord(region)},
                               spatial={"r": region})

            futures = [
                run.scope_complete(spatial={"r": r})
                for r in ["A", "B", "C"]
            ]
            results = {f.result(timeout=30)["sum"] for f in futures}

        self.assertEqual(results, {65 * 3, 66 * 3, 67 * 3})

    def test_scope_with_complex_data(self):
        """Complex Python types survive accumulation across scope boundary."""
        _temp_step("""
            def run(pd, **p):
                pd["data"] = {
                    "tuple": (1, 2),
                    "set": {3, 4},
                    "nested": {"a": [None, True, 2.5]},
                }
                return pd
        """, name="isc_cx1")
        _temp_step("""
            def run(pd, **p):
                first = pd["results"][0]["data"]
                pd["echo"] = first
                return pd
        """, name="isc_cx2")
        yaml = _temp_yaml("""
            wf:
              - isc_cx1:
              - isc_cx2:
                  scope:
                    spatial: r
        """)

        from engine import PipelineEngine
        with PipelineEngine() as e:
            run = e.create_run(yaml)
            run.submit("j", {}, spatial={"r": "1"})
            r = run.scope_complete(spatial={"r": "1"}).result(timeout=30)

        self.assertEqual(r["echo"]["tuple"], (1, 2))
        self.assertEqual(r["echo"]["set"], {3, 4})
        self.assertEqual(r["echo"]["nested"]["a"][2], 2.5)


class TestIntegrationMultiRun(unittest.TestCase):
    """End-to-end tests for multiple interleaved runs."""

    def test_two_runs_interleaved_submits(self):
        """Two runs submit jobs to the same engine interleaved."""
        _temp_step("""
            def run(pd, **p):
                pd["type"] = p.get("type", "unknown")
                pd["id"] = pd["input"]["id"]
                return pd
        """, name="imr_step")
        yaml = _temp_yaml("wf:\n  - imr_step:")

        from engine import PipelineEngine
        with PipelineEngine() as e:
            run_a = e.create_run(yaml, priority="high")
            run_b = e.create_run(yaml)

            futures = []
            for i in range(4):
                futures.append(("A", run_a.submit(f"a{i}", {"id": f"A{i}"})))
                futures.append(("B", run_b.submit(f"b{i}", {"id": f"B{i}"})))

            for expected_type, f in futures:
                r = f.result(timeout=30)
                self.assertTrue(r["id"].startswith(expected_type))

    def test_two_runs_with_scopes(self):
        """Two scoped runs share an engine, each with independent scopes."""
        _temp_step("""
            def run(pd, **p):
                pd["v"] = pd["input"]["v"]
                return pd
        """, name="imr_s1")
        _temp_step("""
            def run(pd, **p):
                pd["sum"] = sum(r["v"] for r in pd["results"])
                pd["run"] = p.get("run_id", "?")
                return pd
        """, name="imr_s2")
        yaml_a = _temp_yaml("""
            wf:
              - imr_s1:
              - imr_s2:
                  scope:
                    spatial: region
                  run_id: A
        """)
        yaml_b = _temp_yaml("""
            wf:
              - imr_s1:
              - imr_s2:
                  scope:
                    spatial: region
                  run_id: B
        """)

        from engine import PipelineEngine
        with PipelineEngine() as e:
            run_a = e.create_run(yaml_a, priority="high")
            run_b = e.create_run(yaml_b)

            run_a.submit("a1", {"v": 1}, spatial={"region": "R"})
            run_a.submit("a2", {"v": 2}, spatial={"region": "R"})
            run_b.submit("b1", {"v": 10}, spatial={"region": "R"})
            run_b.submit("b2", {"v": 20}, spatial={"region": "R"})

            ra = run_a.scope_complete(spatial={"region": "R"}).result(timeout=30)
            rb = run_b.scope_complete(spatial={"region": "R"}).result(timeout=30)

        self.assertEqual(ra["sum"], 3)
        self.assertEqual(ra["run"], "A")
        self.assertEqual(rb["sum"], 30)
        self.assertEqual(rb["run"], "B")

    def test_engine_status_during_work(self):
        """Status reflects active runs and completed work."""
        _temp_step("def run(pd, **p): return pd", name="imr_st")
        yaml = _temp_yaml("wf:\n  - imr_st:")

        from engine import PipelineEngine
        with PipelineEngine() as e:
            run_a = e.create_run(yaml, priority="high")
            run_b = e.create_run(yaml)
            run_a.submit("a1", {}).result(timeout=10)
            run_b.submit("b1", {}).result(timeout=10)

            s = e.status()
            self.assertEqual(len(s["runs"]), 2)
            self.assertEqual(s["runs"][0]["priority"], 10)
            self.assertEqual(s["runs"][0]["completed"], 1)
            self.assertEqual(s["runs"][1]["completed"], 1)

    def test_run_failure_doesnt_affect_other_run(self):
        """A failure in one run doesn't block another run."""
        _temp_step('def run(pd, **p): raise RuntimeError("boom")',
                   name="imr_bad")
        _temp_step("def run(pd, **p): pd['ok'] = True; return pd",
                   name="imr_ok")
        bad_yaml = _temp_yaml("wf:\n  - imr_bad:")
        ok_yaml = _temp_yaml("wf:\n  - imr_ok:")

        from engine import PipelineEngine
        with PipelineEngine() as e:
            run_bad = e.create_run(bad_yaml)
            run_ok = e.create_run(ok_yaml)

            f_bad = run_bad.submit("bad", {})
            f_ok = run_ok.submit("ok", {})

            with self.assertRaises(RuntimeError):
                f_bad.result(timeout=10)
            self.assertTrue(f_ok.result(timeout=10)["ok"])


class TestIntegrationIsolation(unittest.TestCase):
    """Integration tests for isolation settings."""

    def test_maximal_isolation_different_pids(self):
        """Under maximal isolation, each step gets its own process."""
        _temp_step("""
            import os
            def run(pd, **p):
                pd["pid1"] = os.getpid()
                return pd
        """, name="iso_max1")
        _temp_step("""
            import os
            def run(pd, **p):
                pd["pid2"] = os.getpid()
                return pd
        """, name="iso_max2")

        # Force isolation by setting a non-local environment
        # that matches the current env (so it still works, but isolation=maximal
        # forces subprocess execution)
        current_env = Path(sys.prefix).name
        functions_dir = Path(_TEMP_DIR).as_posix()
        yaml_content = (
            f'metadata:\n'
            f'  functions_dir: "{functions_dir}"\n'
            f'  isolation: maximal\n'
            f'  environment: "{current_env}"\n'
            f'wf:\n'
            f'  - iso_max1:\n'
            f'  - iso_max2:\n'
        )
        yaml_path = Path(_TEMP_DIR) / f"pipeline_{_next_id()}.yaml"
        yaml_path.write_text(yaml_content)

        from engine import PipelineEngine
        with PipelineEngine() as e:
            run = e.create_run(str(yaml_path))
            r = run.submit("t", {}).result(timeout=30)

        # Under maximal + pipeline env override, both steps run in subprocesses
        self.assertIn("pid1", r)
        self.assertIn("pid2", r)
        self.assertNotEqual(r["pid1"], os.getpid())
        self.assertNotEqual(r["pid2"], os.getpid())

    def test_minimal_isolation_default(self):
        """Default (minimal) keeps local steps in-process."""
        _temp_step("""
            import os
            def run(pd, **p):
                pd["pid"] = os.getpid()
                return pd
        """, name="iso_min")
        yaml = _temp_yaml("wf:\n  - iso_min:")

        from engine import PipelineEngine
        with PipelineEngine() as e:
            r = e.create_run(yaml).submit("t", {}).result(timeout=10)

        self.assertEqual(r["pid"], os.getpid())


# ── Integration: real environment isolation ───────────────────

# Auto-detect test environments
_TEST_ENV_B = "SMART--basic_test--env_b"
_TEST_ENV_C = "SMART--basic_test--env_c"
try:
    from engine.conda_utils import get_conda_info, env_exists
    _conda_info = get_conda_info()
    _ENV_B_EXISTS = env_exists(_conda_info, _TEST_ENV_B)
    _ENV_C_EXISTS = env_exists(_conda_info, _TEST_ENV_C)
except Exception:
    _ENV_B_EXISTS = False
    _ENV_C_EXISTS = False


@unittest.skipUnless(_ENV_B_EXISTS, f"Requires {_TEST_ENV_B}")
class TestRealIsolation(unittest.TestCase):
    """Integration tests with real conda environment subprocess isolation."""

    def test_step_runs_in_isolated_env(self):
        _temp_step(f"""
            METADATA = {{"environment": "{_TEST_ENV_B}"}}
            def run(pd, **p):
                import sys, os
                pd["env"] = os.path.basename(sys.prefix)
                pd["pid"] = os.getpid()
                return pd
        """, name="ri_single")
        yaml = _temp_yaml("wf:\n  - ri_single:")

        from engine import PipelineEngine
        with PipelineEngine() as e:
            r = e.create_run(yaml).submit("t", {}).result(timeout=60)
        self.assertEqual(r["env"], _TEST_ENV_B)
        self.assertNotEqual(r["pid"], os.getpid())

    def test_local_to_isolated_data_flow(self):
        _temp_step("""
            def run(pd, **p):
                pd["from_local"] = 42
                return pd
        """, name="ri_local")
        _temp_step(f"""
            METADATA = {{"environment": "{_TEST_ENV_B}"}}
            def run(pd, **p):
                pd["from_isolated"] = pd["from_local"] * 2
                return pd
        """, name="ri_iso")
        yaml = _temp_yaml("wf:\n  - ri_local:\n  - ri_iso:")

        from engine import PipelineEngine
        with PipelineEngine() as e:
            r = e.create_run(yaml).submit("t", {}).result(timeout=60)
        self.assertEqual(r["from_local"], 42)
        self.assertEqual(r["from_isolated"], 84)

    def test_isolated_error_wraps_in_step_execution_error(self):
        _temp_step(f"""
            METADATA = {{"environment": "{_TEST_ENV_B}"}}
            def run(pd, **p): raise ValueError("isolated boom")
        """, name="ri_err")
        yaml = _temp_yaml("wf:\n  - ri_err:")

        from engine import PipelineEngine
        with PipelineEngine() as e:
            with self.assertRaises(StepExecutionError) as ctx:
                e.create_run(yaml).submit("t", {}).result(timeout=60)
        self.assertIn("isolated boom", str(ctx.exception))
        self.assertIn("ValueError", ctx.exception.remote_traceback)

    def test_per_env_worker_reuses_process(self):
        """Persistent worker reused across pipeline runs."""
        _temp_step(f"""
            METADATA = {{"environment": "{_TEST_ENV_B}"}}
            def run(pd, **p):
                import os
                pd["pid"] = os.getpid()
                return pd
        """, name="ri_persist")
        yaml = _temp_yaml("wf:\n  - ri_persist:")

        from engine import PipelineEngine
        with PipelineEngine() as e:
            run = e.create_run(yaml)
            r1 = run.submit("t1", {}).result(timeout=60)
            r2 = run.submit("t2", {}).result(timeout=60)
        self.assertEqual(r1["pid"], r2["pid"])

    def test_scoped_pipeline_with_isolation(self):
        """Full scoped pipeline with real env isolation."""
        _temp_step("""
            def run(pd, **p):
                pd["v"] = pd["input"]["v"]
                return pd
        """, name="ri_sc1")
        _temp_step(f"""
            METADATA = {{"environment": "{_TEST_ENV_B}"}}
            def run(pd, **p):
                pd["total"] = sum(r["v"] for r in pd["results"])
                return pd
        """, name="ri_sc2")
        yaml = _temp_yaml("""
            wf:
              - ri_sc1:
              - ri_sc2:
                  scope:
                    spatial: r
        """)

        from engine import PipelineEngine
        with PipelineEngine() as e:
            run = e.create_run(yaml)
            run.submit("a", {"v": 10}, spatial={"r": "R"})
            run.submit("b", {"v": 20}, spatial={"r": "R"})
            r = run.scope_complete(spatial={"r": "R"}).result(timeout=60)
        self.assertEqual(r["total"], 30)


# ── Stress tests ──────────────────────────────────────────────


class TestStress(unittest.TestCase):
    """High-load tests for concurrency, throughput, and stability."""

    def test_50_concurrent_jobs(self):
        """50 jobs submitted simultaneously, all complete correctly."""
        _temp_step("""
            def run(pd, **p):
                pd["id"] = pd["input"]["id"]
                return pd
        """, name="stress_50")
        yaml = _temp_yaml("wf:\n  - stress_50:")

        from engine import PipelineEngine
        n = 50
        with PipelineEngine(max_concurrent=n) as e:
            run = e.create_run(yaml)
            futures = [run.submit(f"j{i}", {"id": i}) for i in range(n)]
            results = [f.result(timeout=60) for f in futures]

        ids = sorted(r["id"] for r in results)
        self.assertEqual(ids, list(range(n)))

    def test_20_scope_groups(self):
        """20 independent scope groups, each with 5 jobs."""
        _temp_step("""
            def run(pd, **p):
                pd["v"] = pd["input"]["v"]
                return pd
        """, name="stress_sg1")
        _temp_step("""
            def run(pd, **p):
                pd["sum"] = sum(r["v"] for r in pd["results"])
                return pd
        """, name="stress_sg2")
        yaml = _temp_yaml("""
            wf:
              - stress_sg1:
              - stress_sg2:
                  scope:
                    spatial: group
        """)

        from engine import PipelineEngine
        n_groups = 20
        jobs_per_group = 5

        with PipelineEngine(max_concurrent=32) as e:
            run = e.create_run(yaml)

            for g in range(n_groups):
                for j in range(jobs_per_group):
                    run.submit(f"g{g}_j{j}", {"v": g},
                               spatial={"group": str(g)})

            futures = [
                run.scope_complete(spatial={"group": str(g)})
                for g in range(n_groups)
            ]
            results = [f.result(timeout=60) for f in futures]

        for g, r in enumerate(results):
            self.assertEqual(r["sum"], g * jobs_per_group)

    def test_rapid_submit_and_scope_complete(self):
        """Submit + scope_complete in tight loop, no race conditions."""
        _temp_step("""
            def run(pd, **p):
                pd["v"] = pd["input"]["v"]
                return pd
        """, name="stress_rapid1")
        _temp_step("""
            def run(pd, **p):
                pd["total"] = sum(r["v"] for r in pd["results"])
                return pd
        """, name="stress_rapid2")
        yaml = _temp_yaml("""
            wf:
              - stress_rapid1:
              - stress_rapid2:
                  scope:
                    spatial: r
        """)

        from engine import PipelineEngine
        with PipelineEngine(max_concurrent=16) as e:
            run = e.create_run(yaml)
            scope_futures = []

            for batch in range(10):
                run.submit(f"b{batch}_0", {"v": 1},
                           spatial={"r": str(batch)})
                run.submit(f"b{batch}_1", {"v": 2},
                           spatial={"r": str(batch)})
                scope_futures.append(
                    run.scope_complete(spatial={"r": str(batch)}))

            results = [f.result(timeout=60) for f in scope_futures]

        for r in results:
            self.assertEqual(r["total"], 3)

    def test_concurrent_multi_step_jobs(self):
        """10 concurrent 4-step pipelines."""
        _temp_step("""
            def run(pd, **p):
                pd["s1"] = pd["input"]["id"]
                return pd
        """, name="stress_ms1")
        _temp_step("""
            def run(pd, **p):
                pd["s2"] = pd["s1"] * 2
                return pd
        """, name="stress_ms2")
        _temp_step("""
            def run(pd, **p):
                pd["s3"] = pd["s2"] + 1
                return pd
        """, name="stress_ms3")
        _temp_step("""
            def run(pd, **p):
                pd["s4"] = pd["s3"] ** 2
                return pd
        """, name="stress_ms4")
        yaml = _temp_yaml("""
            wf:
              - stress_ms1:
              - stress_ms2:
              - stress_ms3:
              - stress_ms4:
        """)

        from engine import PipelineEngine
        n = 10
        with PipelineEngine(max_concurrent=n) as e:
            run = e.create_run(yaml)
            futures = [run.submit(f"j{i}", {"id": i}) for i in range(n)]
            results = [f.result(timeout=30) for f in futures]

        for i, r in enumerate(results):
            self.assertEqual(r["s1"], i)
            self.assertEqual(r["s2"], i * 2)
            self.assertEqual(r["s3"], i * 2 + 1)
            self.assertEqual(r["s4"], (i * 2 + 1) ** 2)

    def test_interleaved_runs_stress(self):
        """3 runs × 10 jobs each, submitted interleaved."""
        _temp_step("""
            def run(pd, **p):
                pd["run"] = pd["input"]["run"]
                pd["idx"] = pd["input"]["idx"]
                return pd
        """, name="stress_il")
        yaml = _temp_yaml("wf:\n  - stress_il:")

        from engine import PipelineEngine
        with PipelineEngine(max_concurrent=32) as e:
            runs = [e.create_run(yaml, priority=p) for p in [10, 0, -10]]
            futures = []

            for idx in range(10):
                for run_id, run in enumerate(runs):
                    futures.append(
                        run.submit(f"r{run_id}_j{idx}",
                                   {"run": run_id, "idx": idx}))

            results = [f.result(timeout=60) for f in futures]

        self.assertEqual(len(results), 30)
        for r in results:
            self.assertIn("run", r)
            self.assertIn("idx", r)


# ── Performance verification ──────────────────────────────────


class TestPerformance(unittest.TestCase):
    """Verify that concurrency, warm workers, and isolation provide
    measurable performance characteristics."""

    def test_concurrent_faster_than_sequential(self):
        """Concurrent submission of independent jobs is faster than serial."""
        _temp_step("""
            import time
            def run(pd, **p):
                time.sleep(0.2)
                pd["done"] = True
                return pd
        """, name="perf_conc")
        yaml = _temp_yaml("wf:\n  - perf_conc:")

        from engine import PipelineEngine
        n = 5

        # Sequential
        with PipelineEngine() as e:
            run = e.create_run(yaml)
            t0 = time.monotonic()
            for i in range(n):
                run.submit(f"seq_{i}", {}).result(timeout=30)
            t_seq = time.monotonic() - t0

        # Concurrent
        with PipelineEngine(max_concurrent=n) as e:
            run = e.create_run(yaml)
            t0 = time.monotonic()
            futures = [run.submit(f"par_{i}", {}) for i in range(n)]
            for f in futures:
                f.result(timeout=30)
            t_par = time.monotonic() - t0

        self.assertLess(t_par, t_seq * 0.7,
                        f"Parallel ({t_par:.2f}s) should be significantly "
                        f"faster than sequential ({t_seq:.2f}s)")

    def test_persistent_worker_warm_start(self):
        """Second call on a persistent worker is faster than the first."""
        from engine._worker import Worker
        path = _temp_step("""
            import time
            def run(pd, **p):
                pd["t"] = time.monotonic()
                return pd
        """)
        w = Worker("local", oneshot=False, connect_timeout=10)
        try:
            t0 = time.monotonic()
            w.execute(path, {}, {}, timeout=10)
            cold = time.monotonic() - t0

            t0 = time.monotonic()
            w.execute(path, {}, {}, timeout=10)
            warm = time.monotonic() - t0

            self.assertLess(warm, cold,
                            f"Warm ({warm:.3f}s) should be faster "
                            f"than cold ({cold:.3f}s)")
        finally:
            w.shutdown()

    def test_minimal_faster_than_maximal(self):
        """Minimal isolation avoids subprocess overhead for local steps."""
        _temp_step("def run(pd, **p): pd['ok'] = True; return pd",
                   name="perf_iso")

        current_env = Path(sys.prefix).name
        functions_dir = Path(_TEMP_DIR).as_posix()

        def make_yaml(isolation):
            content = (
                f'metadata:\n'
                f'  functions_dir: "{functions_dir}"\n'
                f'  isolation: {isolation}\n'
                f'  environment: "{current_env}"\n'
                f'wf:\n'
                f'  - perf_iso:\n'
            )
            p = Path(_TEMP_DIR) / f"perf_{isolation}_{_next_id()}.yaml"
            p.write_text(content)
            return str(p)

        yaml_min = make_yaml("minimal")
        yaml_max = make_yaml("maximal")
        n = 3

        from engine import PipelineEngine

        # Minimal
        with PipelineEngine() as e:
            run = e.create_run(yaml_min)
            t0 = time.monotonic()
            for i in range(n):
                run.submit(f"min_{i}", {}).result(timeout=30)
            t_min = time.monotonic() - t0

        # Maximal (forces subprocess for every step)
        with PipelineEngine() as e:
            run = e.create_run(yaml_max)
            t0 = time.monotonic()
            for i in range(n):
                run.submit(f"max_{i}", {}).result(timeout=30)
            t_max = time.monotonic() - t0

        self.assertLess(t_min, t_max,
                        f"Minimal ({t_min:.2f}s) should be faster "
                        f"than maximal ({t_max:.2f}s)")


# ── Concurrency correctness ──────────────────────────────────


class TestConcurrencyCorrectness(unittest.TestCase):
    """Verify data isolation, thread safety, and absence of race conditions."""

    def test_no_data_cross_contamination(self):
        """Concurrent jobs must not leak data between each other."""
        _temp_step("""
            def run(pd, **p):
                pd["unique"] = pd["input"]["secret"]
                return pd
        """, name="cc_iso")
        yaml = _temp_yaml("wf:\n  - cc_iso:")

        from engine import PipelineEngine
        n = 20
        with PipelineEngine(max_concurrent=n) as e:
            run = e.create_run(yaml)
            futures = [
                run.submit(f"j{i}", {"secret": f"value_{i}"})
                for i in range(n)
            ]
            results = [f.result(timeout=30) for f in futures]

        for i, r in enumerate(results):
            self.assertEqual(r["unique"], f"value_{i}",
                             f"Job {i} got wrong data: {r['unique']}")

    def test_scope_accumulation_thread_safe(self):
        """Many concurrent jobs accumulating into the same scope group."""
        _temp_step("""
            def run(pd, **p):
                pd["v"] = pd["input"]["v"]
                return pd
        """, name="cc_acc")
        _temp_step("""
            def run(pd, **p):
                pd["values"] = sorted(r["v"] for r in pd["results"])
                return pd
        """, name="cc_coll")
        yaml = _temp_yaml("""
            wf:
              - cc_acc:
              - cc_coll:
                  scope:
                    spatial: r
        """)

        from engine import PipelineEngine
        n = 30
        with PipelineEngine(max_concurrent=n) as e:
            run = e.create_run(yaml)
            for i in range(n):
                run.submit(f"j{i}", {"v": i}, spatial={"r": "G"})
            r = run.scope_complete(spatial={"r": "G"}).result(timeout=60)

        self.assertEqual(r["values"], list(range(n)))

    def test_submit_and_scope_complete_race(self):
        """scope_complete waits for jobs still running Phase 0."""
        _temp_step("""
            import time
            def run(pd, **p):
                time.sleep(0.3)
                pd["v"] = pd["input"]["v"]
                return pd
        """, name="cc_race")
        _temp_step("""
            def run(pd, **p):
                pd["sum"] = sum(r["v"] for r in pd["results"])
                return pd
        """, name="cc_race_coll")
        yaml = _temp_yaml("""
            wf:
              - cc_race:
              - cc_race_coll:
                  scope:
                    spatial: r
        """)

        from engine import PipelineEngine
        with PipelineEngine() as e:
            run = e.create_run(yaml)
            run.submit("j1", {"v": 10}, spatial={"r": "X"})
            run.submit("j2", {"v": 20}, spatial={"r": "X"})
            # Call scope_complete immediately — jobs still running
            r = run.scope_complete(spatial={"r": "X"}).result(timeout=30)

        self.assertEqual(r["sum"], 30)

    def test_concurrent_scope_completes_different_groups(self):
        """Multiple scope_complete calls for different groups simultaneously."""
        _temp_step("""
            def run(pd, **p):
                pd["g"] = pd["input"]["g"]
                return pd
        """, name="cc_csc1")
        _temp_step("""
            def run(pd, **p):
                pd["total"] = sum(r["g"] for r in pd["results"])
                return pd
        """, name="cc_csc2")
        yaml = _temp_yaml("""
            wf:
              - cc_csc1:
              - cc_csc2:
                  scope:
                    spatial: r
        """)

        from engine import PipelineEngine
        n_groups = 10
        with PipelineEngine(max_concurrent=32) as e:
            run = e.create_run(yaml)
            for g in range(n_groups):
                for j in range(3):
                    run.submit(f"g{g}_j{j}", {"g": g},
                               spatial={"r": str(g)})

            # Fire all scope_completes concurrently
            scope_futures = [
                run.scope_complete(spatial={"r": str(g)})
                for g in range(n_groups)
            ]
            results = [f.result(timeout=60) for f in scope_futures]

        for g, r in enumerate(results):
            self.assertEqual(r["total"], g * 3)

    def test_pipeline_data_not_shared_between_jobs(self):
        """In-place mutations in one job don't affect another."""
        _temp_step("""
            def run(pd, **p):
                # Mutate input in-place (bad practice, but should be safe)
                pd["input"]["mutated"] = True
                pd["saw_mutated"] = pd["input"].get("mutated")
                return pd
        """, name="cc_mut")
        yaml = _temp_yaml("wf:\n  - cc_mut:")

        from engine import PipelineEngine
        with PipelineEngine() as e:
            run = e.create_run(yaml)
            f1 = run.submit("j1", {"key": "a"})
            f2 = run.submit("j2", {"key": "b"})
            r1 = f1.result(timeout=10)
            r2 = f2.result(timeout=10)

        # Each job saw its own mutation, not the other's
        self.assertEqual(r1["input"]["key"], "a")
        self.assertEqual(r2["input"]["key"], "b")


# ── Scope boundary edge cases ────────────────────────────────


class TestScopeBoundaryEdgeCases(unittest.TestCase):
    """Edge cases at scope boundaries: single jobs, large accumulations,
    order preservation, degenerate cases."""

    def test_single_job_scope(self):
        """Scope with only one job — results list has one element."""
        _temp_step("def run(pd, **p): pd['v'] = 1; return pd",
                   name="sb_s1")
        _temp_step("""
            def run(pd, **p):
                pd["n"] = len(pd["results"])
                pd["v"] = pd["results"][0]["v"]
                return pd
        """, name="sb_s2")
        yaml = _temp_yaml("""
            wf:
              - sb_s1:
              - sb_s2:
                  scope:
                    spatial: r
        """)

        from engine import PipelineEngine
        with PipelineEngine() as e:
            run = e.create_run(yaml)
            run.submit("only", {}, spatial={"r": "X"})
            r = run.scope_complete(spatial={"r": "X"}).result(timeout=10)

        self.assertEqual(r["n"], 1)
        self.assertEqual(r["v"], 1)

    def test_submission_order_preserved(self):
        """Results list at scope boundary preserves submission order."""
        _temp_step("""
            import time
            def run(pd, **p):
                # Varying delays to test that order isn't affected by speed
                time.sleep(0.05 * (5 - pd["input"]["idx"]))
                pd["idx"] = pd["input"]["idx"]
                return pd
        """, name="sb_ord1")
        _temp_step("""
            def run(pd, **p):
                pd["order"] = [r["idx"] for r in pd["results"]]
                return pd
        """, name="sb_ord2")
        yaml = _temp_yaml("""
            wf:
              - sb_ord1:
              - sb_ord2:
                  scope:
                    spatial: r
        """)

        from engine import PipelineEngine
        with PipelineEngine() as e:
            run = e.create_run(yaml)
            for i in range(6):
                run.submit(f"j{i}", {"idx": i}, spatial={"r": "X"})
            r = run.scope_complete(spatial={"r": "X"}).result(timeout=30)

        self.assertEqual(r["order"], [0, 1, 2, 3, 4, 5])

    def test_large_accumulation(self):
        """100 jobs accumulated at a scope boundary."""
        _temp_step("""
            def run(pd, **p):
                pd["v"] = pd["input"]["v"]
                return pd
        """, name="sb_lg1")
        _temp_step("""
            def run(pd, **p):
                pd["count"] = len(pd["results"])
                pd["sum"] = sum(r["v"] for r in pd["results"])
                return pd
        """, name="sb_lg2")
        yaml = _temp_yaml("""
            wf:
              - sb_lg1:
              - sb_lg2:
                  scope:
                    spatial: r
        """)

        from engine import PipelineEngine
        n = 100
        with PipelineEngine(max_concurrent=32) as e:
            run = e.create_run(yaml)
            for i in range(n):
                run.submit(f"j{i}", {"v": i}, spatial={"r": "X"})
            r = run.scope_complete(spatial={"r": "X"}).result(timeout=120)

        self.assertEqual(r["count"], n)
        self.assertEqual(r["sum"], sum(range(n)))

    def test_scope_with_empty_input(self):
        """Jobs with empty input accumulate correctly."""
        _temp_step("def run(pd, **p): pd['x'] = 1; return pd",
                   name="sb_ei1")
        _temp_step("""
            def run(pd, **p):
                pd["n"] = len(pd["results"])
                return pd
        """, name="sb_ei2")
        yaml = _temp_yaml("""
            wf:
              - sb_ei1:
              - sb_ei2:
                  scope:
                    spatial: r
        """)

        from engine import PipelineEngine
        with PipelineEngine() as e:
            run = e.create_run(yaml)
            run.submit("j1", {}, spatial={"r": "X"})
            run.submit("j2", None, spatial={"r": "X"})
            r = run.scope_complete(spatial={"r": "X"}).result(timeout=10)

        self.assertEqual(r["n"], 2)

    def test_unequal_scope_groups(self):
        """Scope groups with different numbers of jobs."""
        _temp_step("def run(pd, **p): pd['v'] = 1; return pd",
                   name="sb_ug1")
        _temp_step("""
            def run(pd, **p):
                pd["count"] = len(pd["results"])
                return pd
        """, name="sb_ug2")
        yaml = _temp_yaml("""
            wf:
              - sb_ug1:
              - sb_ug2:
                  scope:
                    spatial: r
        """)

        from engine import PipelineEngine
        with PipelineEngine() as e:
            run = e.create_run(yaml)
            # Group A: 1 job, Group B: 5 jobs
            run.submit("a1", {}, spatial={"r": "A"})
            for i in range(5):
                run.submit(f"b{i}", {}, spatial={"r": "B"})

            ra = run.scope_complete(spatial={"r": "A"}).result(timeout=30)
            rb = run.scope_complete(spatial={"r": "B"}).result(timeout=30)

        self.assertEqual(ra["count"], 1)
        self.assertEqual(rb["count"], 5)


# ── Error handling under concurrency ─────────────────────────


class TestErrorsUnderConcurrency(unittest.TestCase):
    """Error handling in concurrent and scoped contexts."""

    def test_mid_pipeline_failure(self):
        """Step 2 of 4 fails — steps 3 and 4 never run."""
        _temp_step("def run(pd, **p): pd['s1'] = True; return pd",
                   name="euc_s1")
        _temp_step('def run(pd, **p): raise ValueError("step2 boom")',
                   name="euc_s2")
        _temp_step("def run(pd, **p): pd['s3'] = True; return pd",
                   name="euc_s3")
        _temp_step("def run(pd, **p): pd['s4'] = True; return pd",
                   name="euc_s4")
        yaml = _temp_yaml("""
            wf:
              - euc_s1:
              - euc_s2:
              - euc_s3:
              - euc_s4:
        """)

        from engine import run_pipeline
        with self.assertRaises(ValueError) as ctx:
            run_pipeline(yaml, "t", {})
        self.assertIn("step2 boom", str(ctx.exception))

    def test_one_job_fails_others_succeed(self):
        """In concurrent jobs, one failure doesn't affect others."""
        _temp_step("""
            def run(pd, **p):
                if pd["input"].get("fail"):
                    raise RuntimeError("intentional")
                pd["ok"] = True
                return pd
        """, name="euc_part")
        yaml = _temp_yaml("wf:\n  - euc_part:")

        from engine import PipelineEngine
        with PipelineEngine() as e:
            run = e.create_run(yaml)
            f_good1 = run.submit("good1", {"fail": False})
            f_bad = run.submit("bad", {"fail": True})
            f_good2 = run.submit("good2", {"fail": False})

            self.assertTrue(f_good1.result(timeout=10)["ok"])
            with self.assertRaises(RuntimeError):
                f_bad.result(timeout=10)
            self.assertTrue(f_good2.result(timeout=10)["ok"])

    def test_error_in_scoped_step(self):
        """Error during scoped phase execution is propagated."""
        _temp_step("def run(pd, **p): pd['v'] = 1; return pd",
                   name="euc_es1")
        _temp_step("""
            def run(pd, **p):
                raise ValueError("scoped step failed")
        """, name="euc_es2")
        yaml = _temp_yaml("""
            wf:
              - euc_es1:
              - euc_es2:
                  scope:
                    spatial: r
        """)

        from engine import PipelineEngine
        with PipelineEngine() as e:
            run = e.create_run(yaml)
            run.submit("j1", {}, spatial={"r": "X"})
            with self.assertRaises(ValueError):
                run.scope_complete(spatial={"r": "X"}).result(timeout=10)

    def test_wrong_return_type_in_scoped_phase(self):
        """Non-dict return in a scoped step raises TypeError."""
        _temp_step("def run(pd, **p): pd['v'] = 1; return pd",
                   name="euc_rt1")
        _temp_step("def run(pd, **p): return [1, 2, 3]",
                   name="euc_rt2")
        yaml = _temp_yaml("""
            wf:
              - euc_rt1:
              - euc_rt2:
                  scope:
                    spatial: r
        """)

        from engine import PipelineEngine
        with PipelineEngine() as e:
            run = e.create_run(yaml)
            run.submit("j1", {}, spatial={"r": "X"})
            with self.assertRaises(TypeError):
                run.scope_complete(spatial={"r": "X"}).result(timeout=10)

    def test_scope_complete_after_job_failure(self):
        """scope_complete propagates failure if a job in the group failed."""
        _temp_step("""
            def run(pd, **p):
                if pd["input"].get("fail"):
                    raise RuntimeError("job failed")
                pd["v"] = 1
                return pd
        """, name="euc_sf")
        _temp_step("def run(pd, **p): return pd", name="euc_sf2")
        yaml = _temp_yaml("""
            wf:
              - euc_sf:
              - euc_sf2:
                  scope:
                    spatial: r
        """)

        from engine import PipelineEngine
        with PipelineEngine() as e:
            run = e.create_run(yaml)
            run.submit("good", {"fail": False}, spatial={"r": "X"})
            run.submit("bad", {"fail": True}, spatial={"r": "X"})
            with self.assertRaises(RuntimeError):
                run.scope_complete(spatial={"r": "X"}).result(timeout=10)


# ── Worker lifecycle under load ───────────────────────────────


class TestWorkerLifecycle(unittest.TestCase):
    """Worker behavior under realistic load patterns."""

    def test_ten_step_pipeline(self):
        """Pipeline with 10 sequential steps completes correctly."""
        for i in range(10):
            _temp_step(f"""
                def run(pd, **p):
                    pd["step_{i}"] = pd.get("step_{i-1}", 0) + 1 if {i} > 0 else 1
                    return pd
            """, name=f"wl_s{i}")
        steps = "\n".join(f"  - wl_s{i}:" for i in range(10))
        yaml = _temp_yaml(f"wf:\n{steps}")

        from engine import run_pipeline
        r = run_pipeline(yaml, "t", {})
        self.assertEqual(r["step_9"], 10)

    def test_large_data_through_pipeline(self):
        """Large dict flows through multiple steps without corruption."""
        _temp_step("""
            def run(pd, **p):
                pd["big"] = list(range(10000))
                return pd
        """, name="wl_big1")
        _temp_step("""
            def run(pd, **p):
                pd["sum"] = sum(pd["big"])
                pd["len"] = len(pd["big"])
                return pd
        """, name="wl_big2")
        yaml = _temp_yaml("wf:\n  - wl_big1:\n  - wl_big2:")

        from engine import run_pipeline
        r = run_pipeline(yaml, "t", {})
        self.assertEqual(r["len"], 10000)
        self.assertEqual(r["sum"], sum(range(10000)))

    def test_worker_crash_recovery_multi_job(self):
        """Worker recovers from crash and processes subsequent jobs."""
        from engine._worker import Worker
        path = _temp_step("""
            import os
            def run(pd, **p):
                pd["pid"] = os.getpid()
                return pd
        """)
        w = Worker("local", oneshot=False, connect_timeout=10)
        try:
            r1 = w.execute(path, {}, {}, timeout=10)
            pid1 = r1["pid"]

            # Kill the worker
            w._process.kill()
            w._process.wait()
            self.assertFalse(w.is_alive())

            # Should auto-respawn on next call
            r2 = w.execute(path, {}, {}, timeout=10)
            self.assertNotEqual(r2["pid"], pid1)

            # Third call should reuse the respawned worker
            r3 = w.execute(path, {}, {}, timeout=10)
            self.assertEqual(r3["pid"], r2["pid"])
        finally:
            w.shutdown()

    def test_params_vary_per_step(self):
        """Different params for each step are correctly delivered."""
        _temp_step("""
            def run(pd, **p):
                pd["a_val"] = p.get("x")
                return pd
        """, name="wl_pa")
        _temp_step("""
            def run(pd, **p):
                pd["b_val"] = p.get("y")
                return pd
        """, name="wl_pb")
        _temp_step("""
            def run(pd, **p):
                pd["c_val"] = p.get("z")
                return pd
        """, name="wl_pc")
        yaml = _temp_yaml("""
            wf:
              - wl_pa:
                  x: 10
              - wl_pb:
                  y: 20
              - wl_pc:
                  z: 30
        """)

        from engine import run_pipeline
        r = run_pipeline(yaml, "t", {})
        self.assertEqual(r["a_val"], 10)
        self.assertEqual(r["b_val"], 20)
        self.assertEqual(r["c_val"], 30)


# ── Real-world pattern simulations ───────────────────────────


class TestRealWorldPatterns(unittest.TestCase):
    """Simulations of real microscopy analysis workflows."""

    def test_tile_grid_stitch(self):
        """Simulate 3x3 tile grid → segment → stitch."""
        _temp_step("""
            def run(pd, **p):
                row, col = pd["input"]["row"], pd["input"]["col"]
                pd["mask"] = [[row * 3 + col] * 3] * 3
                return pd
        """, name="rw_seg")
        _temp_step("""
            def run(pd, **p):
                tiles = pd["results"]
                grid = {}
                for t in tiles:
                    r, c = t["input"]["row"], t["input"]["col"]
                    grid[(r, c)] = t["mask"][0][0]
                pd["stitched"] = grid
                pd["n_tiles"] = len(tiles)
                return pd
        """, name="rw_stitch")
        yaml = _temp_yaml("""
            wf:
              - rw_seg:
              - rw_stitch:
                  scope:
                    spatial: region
        """)

        from engine import PipelineEngine
        with PipelineEngine() as e:
            run = e.create_run(yaml)
            for row in range(3):
                for col in range(3):
                    run.submit(f"tile_{row}_{col}",
                               {"row": row, "col": col},
                               spatial={"region": "R1"})
            r = run.scope_complete(
                spatial={"region": "R1"}).result(timeout=30)

        self.assertEqual(r["n_tiles"], 9)
        self.assertEqual(r["stitched"][(0, 0)], 0)
        self.assertEqual(r["stitched"][(2, 2)], 8)

    def test_timepoint_series_tracking(self):
        """Simulate T timepoints → detect → track across time.

        All timepoints share the same scope key so they accumulate together.
        """
        _temp_step("""
            def run(pd, **p):
                t = pd["input"]["t"]
                pd["detections"] = [{"x": t * 10 + i, "y": i}
                                    for i in range(3)]
                return pd
        """, name="rw_det")
        _temp_step("""
            def run(pd, **p):
                all_dets = []
                for r in pd["results"]:
                    all_dets.append(r["detections"])
                pd["tracks"] = {
                    "n_timepoints": len(all_dets),
                    "total_detections": sum(len(d) for d in all_dets),
                }
                return pd
        """, name="rw_track")
        yaml = _temp_yaml("""
            wf:
              - rw_det:
              - rw_track:
                  scope:
                    temporal: series
        """)

        from engine import PipelineEngine
        n_timepoints = 5
        with PipelineEngine() as e:
            run = e.create_run(yaml)
            for t in range(n_timepoints):
                run.submit(f"t{t}", {"t": t},
                           temporal={"series": "S1"})
            r = run.scope_complete(
                temporal={"series": "S1"}).result(timeout=30)

        self.assertEqual(r["tracks"]["n_timepoints"], n_timepoints)
        self.assertEqual(r["tracks"]["total_detections"], n_timepoints * 3)

    def test_overview_target_interleaved(self):
        """Simulate interleaved overview and target analysis runs."""
        _temp_step("""
            def run(pd, **p):
                pd["analyzed"] = {
                    "type": pd["input"]["type"],
                    "id": pd["input"]["id"],
                }
                return pd
        """, name="rw_analyze")
        yaml = _temp_yaml("wf:\n  - rw_analyze:")

        from engine import PipelineEngine
        with PipelineEngine() as e:
            ov = e.create_run(yaml, priority="high")
            tg = e.create_run(yaml)

            # Interleave submissions
            futures = []
            for i in range(5):
                futures.append(("overview",
                    ov.submit(f"ov_{i}", {"type": "overview", "id": i})))
                futures.append(("target",
                    tg.submit(f"tg_{i}", {"type": "target", "id": i})))

            for expected_type, f in futures:
                r = f.result(timeout=30)
                self.assertEqual(r["analyzed"]["type"], expected_type)

    def test_overview_target_with_scopes(self):
        """Interleaved runs where overview has scopes and target doesn't."""
        _temp_step("""
            def run(pd, **p):
                pd["v"] = pd["input"]["v"]
                return pd
        """, name="rw_ots1")
        _temp_step("""
            def run(pd, **p):
                pd["total"] = sum(r["v"] for r in pd["results"])
                return pd
        """, name="rw_ots2")
        yaml_ov = _temp_yaml("""
            wf:
              - rw_ots1:
              - rw_ots2:
                  scope:
                    spatial: region
        """)

        _temp_step("""
            def run(pd, **p):
                pd["target_result"] = pd["input"]["pos"]
                return pd
        """, name="rw_ott")
        yaml_tg = _temp_yaml("wf:\n  - rw_ott:")

        from engine import PipelineEngine
        with PipelineEngine() as e:
            ov = e.create_run(yaml_ov, priority="high")
            tg = e.create_run(yaml_tg)

            # Submit overview tiles
            ov.submit("ov1", {"v": 10}, spatial={"region": "R1"})
            ov.submit("ov2", {"v": 20}, spatial={"region": "R1"})

            # Submit target (no scope)
            f_tg = tg.submit("tg1", {"pos": "A1"})

            # Complete overview scope
            f_ov = ov.scope_complete(spatial={"region": "R1"})

            r_ov = f_ov.result(timeout=30)
            r_tg = f_tg.result(timeout=30)

        self.assertEqual(r_ov["total"], 30)
        self.assertEqual(r_tg["target_result"], "A1")

    def test_multi_region_multi_timepoint(self):
        """Multiple regions × multiple timepoints, each with scope completion."""
        _temp_step("""
            def run(pd, **p):
                pd["data"] = {
                    "region": pd["input"]["region"],
                    "tile": pd["input"]["tile"],
                }
                return pd
        """, name="rw_mrmt1")
        _temp_step("""
            def run(pd, **p):
                tiles = [r["data"]["tile"] for r in pd["results"]]
                pd["region_result"] = {
                    "n_tiles": len(tiles),
                    "tiles": sorted(tiles),
                }
                return pd
        """, name="rw_mrmt2")
        yaml = _temp_yaml("""
            wf:
              - rw_mrmt1:
              - rw_mrmt2:
                  scope:
                    spatial: region
        """)

        from engine import PipelineEngine
        regions = ["R1", "R2", "R3"]
        tiles_per_region = 4

        with PipelineEngine(max_concurrent=32) as e:
            run = e.create_run(yaml)

            for region in regions:
                for tile in range(tiles_per_region):
                    run.submit(
                        f"{region}_t{tile}",
                        {"region": region, "tile": tile},
                        spatial={"region": region},
                    )

            results = {}
            for region in regions:
                r = run.scope_complete(
                    spatial={"region": region}).result(timeout=30)
                results[region] = r["region_result"]

        for region in regions:
            self.assertEqual(results[region]["n_tiles"], tiles_per_region)
            self.assertEqual(results[region]["tiles"],
                             list(range(tiles_per_region)))


# ── Extended stress tests ─────────────────────────────────────


class TestStressExtended(unittest.TestCase):
    """Additional stress tests for edge conditions under high load."""

    def test_100_jobs_scope_complete(self):
        """100 jobs into a single scope group."""
        _temp_step("def run(pd, **p): pd['v'] = pd['input']['v']; return pd",
                   name="sx_100a")
        _temp_step("""
            def run(pd, **p):
                pd["sum"] = sum(r["v"] for r in pd["results"])
                pd["n"] = len(pd["results"])
                return pd
        """, name="sx_100b")
        yaml = _temp_yaml("""
            wf:
              - sx_100a:
              - sx_100b:
                  scope:
                    spatial: r
        """)

        from engine import PipelineEngine
        n = 100
        with PipelineEngine(max_concurrent=32) as e:
            run = e.create_run(yaml)
            for i in range(n):
                run.submit(f"j{i}", {"v": i}, spatial={"r": "X"})
            r = run.scope_complete(spatial={"r": "X"}).result(timeout=120)

        self.assertEqual(r["n"], n)
        self.assertEqual(r["sum"], sum(range(n)))

    def test_many_single_job_scopes(self):
        """50 scope groups, each with exactly 1 job."""
        _temp_step("def run(pd, **p): pd['v'] = pd['input']['v']; return pd",
                   name="sx_single1")
        _temp_step("""
            def run(pd, **p):
                pd["result"] = pd["results"][0]["v"]
                return pd
        """, name="sx_single2")
        yaml = _temp_yaml("""
            wf:
              - sx_single1:
              - sx_single2:
                  scope:
                    spatial: r
        """)

        from engine import PipelineEngine
        n = 50
        with PipelineEngine(max_concurrent=32) as e:
            run = e.create_run(yaml)
            for i in range(n):
                run.submit(f"j{i}", {"v": i}, spatial={"r": str(i)})
            futures = [
                run.scope_complete(spatial={"r": str(i)})
                for i in range(n)
            ]
            results = [f.result(timeout=60) for f in futures]

        for i, r in enumerate(results):
            self.assertEqual(r["result"], i)

    def test_rapid_engine_create_destroy(self):
        """Create and destroy engines rapidly — no resource leaks."""
        _temp_step("def run(pd, **p): pd['ok'] = True; return pd",
                   name="sx_rapid")
        yaml = _temp_yaml("wf:\n  - sx_rapid:")

        from engine import PipelineEngine
        for _ in range(10):
            with PipelineEngine() as e:
                r = e.create_run(yaml).submit("t", {}).result(timeout=10)
                self.assertTrue(r["ok"])

    def test_zero_delay_steps_under_concurrency(self):
        """Near-instant steps under high concurrency — no race conditions."""
        _temp_step("def run(pd, **p): pd['i'] = pd['input']['i']; return pd",
                   name="sx_fast")
        yaml = _temp_yaml("wf:\n  - sx_fast:")

        from engine import PipelineEngine
        n = 50
        with PipelineEngine(max_concurrent=n) as e:
            run = e.create_run(yaml)
            futures = [run.submit(f"j{i}", {"i": i}) for i in range(n)]
            results = [f.result(timeout=30) for f in futures]

        self.assertEqual(sorted(r["i"] for r in results), list(range(n)))


# ── Regression ────────────────────────────────────────────────


class TestRegression(unittest.TestCase):
    """Guards against specific past bugs and v3 design guarantees."""

    def test_metadata_extraction_never_executes_code(self):
        path = _temp_step("""
            import nonexistent_package
            raise RuntimeError("must not run")
            METADATA = {"environment": "remote", "device": "gpu"}
        """)
        s = get_step_settings(Path(path))
        self.assertEqual(s["environment"], "remote")
        self.assertEqual(s["device"], "gpu")

    def test_local_error_preserves_original_type(self):
        _temp_step('def run(pd, **p): raise ValueError("local")',
                   name="reg_le")
        yaml = _temp_yaml("wf:\n  - reg_le:")
        from engine import run_pipeline
        with self.assertRaises(ValueError) as ctx:
            run_pipeline(yaml, "t", {})
        self.assertIn("local", str(ctx.exception))

    def test_step_without_metadata_defaults_local_cpu(self):
        _temp_step("def run(pd, **p): pd['ok'] = True; return pd",
                   name="reg_def")
        yaml = _temp_yaml("wf:\n  - reg_def:")
        from engine import run_pipeline
        r = run_pipeline(yaml, "t", {})
        self.assertTrue(r["ok"])

    def test_get_step_settings_returns_dict_not_module(self):
        path = _temp_step('METADATA = {"environment": "test"}')
        s = get_step_settings(Path(path))
        self.assertIsInstance(s, dict)
        self.assertEqual(s["environment"], "test")

    def test_missing_run_fails_at_execution(self):
        _temp_step("x = 1", name="reg_nr")
        yaml = _temp_yaml("wf:\n  - reg_nr:")
        from engine import run_pipeline
        with self.assertRaises(AttributeError):
            run_pipeline(yaml, "t", {})

    def test_v3_version(self):
        import engine
        self.assertTrue(engine.__version__.startswith("3."))


# ── Package API ───────────────────────────────────────────────


class TestPackageAPI(unittest.TestCase):

    def test_public_imports(self):
        from engine import (run_pipeline, PipelineEngine, Run,
                            WorkerError, WorkerSpawnError,
                            WorkerCrashedError, StepExecutionError,
                            ScopeError)
        self.assertTrue(callable(run_pipeline))
        self.assertTrue(callable(PipelineEngine))

    def test_version(self):
        import engine
        self.assertIsInstance(engine.__version__, str)
        self.assertEqual(engine.__version__, "3.0.0")


if __name__ == "__main__":
    unittest.main()
