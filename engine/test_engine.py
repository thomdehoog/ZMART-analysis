"""
Test suite for the unified pipeline engine.

Covers the full stack: module loading, AST-based METADATA extraction,
subprocess workers (oneshot and persistent), worker pool with concurrency
control, pipeline orchestration, multi-job patterns (concurrent, sequential,
adaptive feedback), real environment isolation, and regression guards for
every known past bug.

Structure
---------
- TestErrors                  Exception hierarchy
- TestLoadFunction            exec-based module loading
- TestGetStepSettings         AST-based METADATA extraction
- TestWorkerOneshot           Spawn-run-exit subprocess workers
- TestWorkerPersistent        Warm persistent workers
- TestWorkerErrorPaths        Crash, timeout, missing file
- TestPool                    Concurrency control, reaper, semaphores
- TestPipelineEngine          End-to-end pipeline orchestration
- TestReturnValidation        Step return type enforcement
- TestYAMLEdgeCases           Config parsing edge cases
- TestInputData               Input handling (None, falsy, etc.)
- TestLifecycle               Engine startup/shutdown
- TestMultiJob                Multi-job patterns (local only)
- TestIsolation               Real subprocess isolation (env-dependent)
- TestMultiJobIsolation       Multi-job + isolation (env-dependent)
- TestRegression              Guards against specific past bugs
- TestPackageAPI              Public exports and versioning

Usage
-----
    python -m pytest engine/test_engine.py -v            # all tests
    python -m pytest engine/test_engine.py -k Regression  # just regressions
    python -m pytest engine/test_engine.py -v --tb=short  # compact output

Environment-dependent tests (TestIsolation, TestMultiJobIsolation) auto-skip
if conda environments SMART--basic_test--env_b/env_c are not found.
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
from engine._errors import (
    WorkerError, WorkerSpawnError, WorkerCrashedError, StepExecutionError,
)

# Test fixtures
BASIC_TEST = ENGINE_DIR.parent / "workflows" / "basic_test"
STEPS_DIR = BASIC_TEST / "steps"
PIPELINES_DIR = BASIC_TEST / "pipelines"

# Isolation test environments (auto-detected)
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
        # Forward slashes: valid on all platforms, avoids YAML interpreting
        # backslashes as escape sequences on Windows.
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

    def test_hierarchy(self):
        self.assertTrue(issubclass(WorkerSpawnError, WorkerError))
        self.assertTrue(issubclass(WorkerCrashedError, WorkerError))
        self.assertTrue(issubclass(StepExecutionError, WorkerError))

    def test_step_execution_error_stores_traceback(self):
        err = StepExecutionError("boom", remote_traceback="tb")
        self.assertEqual(str(err), "boom")
        self.assertEqual(err.remote_traceback, "tb")

    def test_step_execution_error_traceback_default_none(self):
        self.assertIsNone(StepExecutionError("x").remote_traceback)


# ── Loader ────────────────────────────────────────────────────


class TestLoadFunction(unittest.TestCase):

    def test_loads_and_runs_step(self):
        """Load a step, verify METADATA, __name__, __file__, and run()."""
        module = load_function("step_local", STEPS_DIR)
        self.assertEqual(module.__name__, "step_local")
        self.assertIsInstance(module.METADATA, dict)
        result = module.run({"metadata": {"verbose": 0}}, test_param="x")
        self.assertIsInstance(result, dict)
        self.assertIn("step_local", result)

    def test_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            load_function("does_not_exist", STEPS_DIR)

    def test_step_with_import_error(self):
        path = _temp_step("import nonexistent_xyz\ndef run(pd, **p): return pd")
        with self.assertRaises(ModuleNotFoundError):
            load_function(Path(path).stem, Path(path).parent)

    def test_syntax_error(self):
        path = _temp_step("def run(\n    return {}")
        with self.assertRaises(SyntaxError):
            load_function(Path(path).stem, Path(path).parent)

    def test_runtime_error_at_module_level(self):
        path = _temp_step("x = 1 / 0\ndef run(pd, **p): return pd")
        with self.assertRaises(ZeroDivisionError):
            load_function(Path(path).stem, Path(path).parent)

    def test_step_missing_run(self):
        """Loader does not enforce run() — returns module without it."""
        path = _temp_step('x = 1')
        module = load_function(Path(path).stem, Path(path).parent)
        self.assertFalse(hasattr(module, "run"))

    def test_module_file_attribute(self):
        path = _temp_step("def run(pd, **p): return pd")
        module = load_function(Path(path).stem, Path(path).parent)
        self.assertEqual(module.__file__, path)


class TestGetStepSettings(unittest.TestCase):

    def test_defaults_no_metadata(self):
        """Step with no METADATA gets all defaults."""
        path = _temp_step("def run(pd, **p): return pd")
        s = get_step_settings(Path(path))
        self.assertEqual(s, {"environment": "local", "worker": "subprocess",
                             "max_workers": 1})

    def test_explicit(self):
        path = _temp_step("""
            METADATA = {
                "environment": "gpu",
                "worker": "persistent",
                "max_workers": 2,
            }
            def run(pd, **p): return pd
        """)
        s = get_step_settings(Path(path))
        self.assertEqual(s["environment"], "gpu")
        self.assertEqual(s["worker"], "persistent")
        self.assertEqual(s["max_workers"], 2)

    def test_partial(self):
        path = _temp_step("""
            METADATA = {"environment": "cpu"}
            def run(pd, **p): return pd
        """)
        s = get_step_settings(Path(path))
        self.assertEqual(s["environment"], "cpu")
        self.assertEqual(s["worker"], "subprocess")

    def test_no_data_transfer_in_output(self):
        path = _temp_step("""
            METADATA = {"description": "test"}
            def run(pd, **p): return pd
        """)
        self.assertNotIn("data_transfer", get_step_settings(Path(path)))

    def test_metadata_none_uses_defaults(self):
        path = _temp_step("METADATA = None\ndef run(pd, **p): return pd")
        s = get_step_settings(Path(path))
        self.assertEqual(s["environment"], "local")

    def test_does_not_execute_module_code(self):
        """Core guarantee: METADATA extraction never runs module-level code."""
        path = _temp_step("""
            import nonexistent_package_xyz
            METADATA = {"environment": "isolated_env", "worker": "persistent"}
            def run(pd, **p): return pd
        """)
        s = get_step_settings(Path(path))
        self.assertEqual(s["environment"], "isolated_env")
        self.assertEqual(s["worker"], "persistent")

    def test_does_not_execute_side_effects(self):
        """Module-level exceptions don't affect extraction."""
        path = _temp_step("""
            raise RuntimeError("should never run")
            METADATA = {"environment": "safe"}
            def run(pd, **p): return pd
        """)
        s = get_step_settings(Path(path))
        self.assertEqual(s["environment"], "safe")


# ── Worker (oneshot) ──────────────────────────────────────────


class TestWorkerOneshot(unittest.TestCase):

    def test_execute_and_return(self):
        from engine._worker import Worker
        path = _temp_step("""
            def run(pipeline_data, **params):
                pipeline_data["ran"] = True
                pipeline_data["x"] = params.get("x")
                return pipeline_data
        """)
        w = Worker("local", path, oneshot=True, connect_timeout=10)
        try:
            result = w.execute({"input": 1}, {"x": 42}, timeout=10)
        finally:
            w.shutdown()
        self.assertEqual(result["ran"], True)
        self.assertEqual(result["x"], 42)
        self.assertEqual(result["input"], 1)

    def test_step_error_raises(self):
        from engine._worker import Worker
        path = _temp_step('def run(pd, **p): raise ValueError("intentional")')
        w = Worker("local", path, oneshot=True, connect_timeout=10)
        try:
            with self.assertRaises(StepExecutionError) as ctx:
                w.execute({}, {}, timeout=10)
            self.assertIn("intentional", str(ctx.exception))
            self.assertIsNotNone(ctx.exception.remote_traceback)
        finally:
            w.shutdown()

    def test_preserves_complex_types(self):
        from engine._worker import Worker
        path = _temp_step("def run(pd, **p): return pd")
        data = {
            "tuple": (1, 2, 3),
            "set": {4, 5, 6},
            "bytes": b"\x00\xff",
            "nested": {"a": [1, None, True, {"b": 2.5}]},
            "none": None,
            "large_int": 10**100,
        }
        w = Worker("local", path, oneshot=True, connect_timeout=10)
        try:
            result = w.execute(data, {}, timeout=10)
        finally:
            w.shutdown()
        self.assertEqual(result["tuple"], (1, 2, 3))
        self.assertEqual(result["set"], {4, 5, 6})
        self.assertEqual(result["bytes"], b"\x00\xff")
        self.assertEqual(result["nested"]["a"][3]["b"], 2.5)
        self.assertIsNone(result["none"])
        self.assertEqual(result["large_int"], 10**100)

    def test_process_exits_after_oneshot(self):
        from engine._worker import Worker
        path = _temp_step("def run(pd, **p): return pd")
        w = Worker("local", path, oneshot=True, connect_timeout=10)
        w.execute({}, {}, timeout=10)
        time.sleep(0.5)
        self.assertFalse(w.is_alive())
        w.shutdown()

    def test_shutdown_after_oneshot_is_safe(self):
        from engine._worker import Worker
        path = _temp_step("def run(pd, **p): return pd")
        w = Worker("local", path, oneshot=True, connect_timeout=10)
        w.execute({}, {}, timeout=10)
        time.sleep(0.3)
        w.shutdown()
        w.shutdown()  # double shutdown

    def test_error_cleans_up_resources(self):
        from engine._worker import Worker
        path = _temp_step('def run(pd, **p): raise RuntimeError("fail")')
        w = Worker("local", path, oneshot=True, connect_timeout=10)
        with self.assertRaises(StepExecutionError):
            w.execute({}, {}, timeout=10)
        w.shutdown()
        self.assertIsNone(w._conn)
        self.assertIsNone(w._listener)
        self.assertIsNone(w._process)

    def test_oneshot_via_pool(self):
        from engine._pool import WorkerPool
        path = _temp_step("""
            def run(pd, **p):
                pd["pooled"] = True
                return pd
        """)
        pool = WorkerPool()
        result = pool.execute("local", path, {}, {},
                              worker_type="subprocess", timeout=10)
        self.assertEqual(result["pooled"], True)
        self.assertEqual(pool.active_workers(), [])
        pool.shutdown_all()


# ── Worker (persistent) ──────────────────────────────────────


class TestWorkerPersistent(unittest.TestCase):

    def test_reuses_same_process(self):
        from engine._worker import Worker
        path = _temp_step("""
            import os
            def run(pd, **p):
                pd["pid"] = os.getpid()
                return pd
        """)
        w = Worker("local", path, oneshot=False, connect_timeout=10)
        try:
            r1 = w.execute({}, {}, timeout=10)
            r2 = w.execute({}, {}, timeout=10)
            self.assertEqual(r1["pid"], r2["pid"])
            self.assertTrue(w.is_alive())
        finally:
            w.shutdown()

    def test_module_stays_loaded_across_calls(self):
        """Persistent worker keeps module state warm between calls.

        A module-level counter increments on each import. If the module
        were reloaded between calls, the counter would reset to 1.
        With a warm worker, it stays at 1 for every call because the
        module is loaded only once.
        """
        from engine._worker import Worker
        path = _temp_step("""
            _load_count = 0

            def run(pd, **p):
                global _load_count
                _load_count += 1
                pd["load_count"] = _load_count
                return pd
        """)
        w = Worker("local", path, oneshot=False, connect_timeout=10)
        try:
            r1 = w.execute({}, {}, timeout=10)
            r2 = w.execute({}, {}, timeout=10)
            r3 = w.execute({}, {}, timeout=10)
            # Module loaded once, counter increments across calls
            self.assertEqual(r1["load_count"], 1)
            self.assertEqual(r2["load_count"], 2)
            self.assertEqual(r3["load_count"], 3)
        finally:
            w.shutdown()

    def test_warm_worker_faster_than_cold(self):
        """Second call on a persistent worker is faster than the first.

        The first call pays startup cost (spawn process, load module).
        Subsequent calls skip all of that — only the run() call happens.
        """
        from engine._worker import Worker
        path = _temp_step("""
            import time
            def run(pd, **p):
                pd["t"] = time.monotonic()
                return pd
        """)
        w = Worker("local", path, oneshot=False, connect_timeout=10)
        try:
            # First call: cold start (spawn + connect + load + run)
            t0 = time.monotonic()
            w.execute({}, {}, timeout=10)
            cold = time.monotonic() - t0

            # Second call: warm (run only)
            t0 = time.monotonic()
            w.execute({}, {}, timeout=10)
            warm = time.monotonic() - t0

            self.assertLess(warm, cold,
                            f"Warm call ({warm:.3f}s) should be faster "
                            f"than cold start ({cold:.3f}s)")
        finally:
            w.shutdown()

    def test_recovers_from_crash(self):
        from engine._worker import Worker
        path = _temp_step("""
            import os
            def run(pd, **p):
                pd["pid"] = os.getpid()
                return pd
        """)
        w = Worker("local", path, oneshot=False, connect_timeout=10)
        try:
            r1 = w.execute({}, {}, timeout=10)
            w._process.kill()
            w._process.wait()
            self.assertFalse(w.is_alive())
            r2 = w.execute({}, {}, timeout=10)
            self.assertNotEqual(r1["pid"], r2["pid"])
        finally:
            w.shutdown()

    def test_idle_resets_on_execute(self):
        from engine._worker import Worker
        path = _temp_step("def run(pd, **p): return pd")
        w = Worker("local", path, oneshot=False,
                   idle_timeout=0.3, connect_timeout=10)
        try:
            w.execute({}, {}, timeout=10)
            time.sleep(0.2)
            self.assertFalse(w.is_idle())
            w.execute({}, {}, timeout=10)
            self.assertFalse(w.is_idle())
            time.sleep(0.4)
            self.assertTrue(w.is_idle())
        finally:
            w.shutdown()

    def test_execute_after_shutdown_respawns(self):
        """Worker respawns if execute() is called after shutdown()."""
        from engine._worker import Worker
        path = _temp_step("""
            import os
            def run(pd, **p):
                pd["pid"] = os.getpid()
                return pd
        """)
        w = Worker("local", path, oneshot=False, connect_timeout=10)
        try:
            r1 = w.execute({}, {}, timeout=10)
            w.shutdown()
            r2 = w.execute({}, {}, timeout=10)
            self.assertNotEqual(r1["pid"], r2["pid"])
        finally:
            w.shutdown()


# ── Worker Error Paths ────────────────────────────────────────


class TestWorkerErrorPaths(unittest.TestCase):

    def test_process_crash_raises_worker_crashed(self):
        from engine._worker import Worker
        path = _temp_step("import os\ndef run(pd, **p): os._exit(1)")
        w = Worker("local", path, oneshot=True, connect_timeout=10)
        with self.assertRaises(WorkerCrashedError):
            w.execute({}, {}, timeout=10)
        w.shutdown()

    def test_timeout_raises_step_execution_error(self):
        from engine._worker import Worker
        path = _temp_step("""
            import time
            def run(pd, **p):
                time.sleep(30)
                return pd
        """)
        w = Worker("local", path, oneshot=True, connect_timeout=10)
        with self.assertRaises(StepExecutionError) as ctx:
            w.execute({}, {}, timeout=1)
        self.assertIn("timed out", str(ctx.exception))
        w.shutdown()

    def test_nonexistent_step_path_raises(self):
        """Worker with a step file that doesn't exist should fail."""
        from engine._worker import Worker
        w = Worker("local", "/nonexistent/step.py",
                   oneshot=True, connect_timeout=3)
        with self.assertRaises((WorkerSpawnError, WorkerCrashedError)):
            w.execute({}, {}, timeout=5)
        w.shutdown()


# ── Pool ──────────────────────────────────────────────────────


class TestPool(unittest.TestCase):

    def test_shutdown_before_use(self):
        from engine._pool import WorkerPool
        pool = WorkerPool()
        pool.shutdown_all()
        self.assertEqual(pool.active_workers(), [])

    def test_semaphore_limits_concurrency(self):
        """With max_workers=1, two calls must run sequentially."""
        from engine._pool import WorkerPool
        path = _temp_step("""
            import time
            def run(pd, **p):
                time.sleep(0.4)
                pd["done"] = True
                return pd
        """)
        pool = WorkerPool()
        results = []

        def run_one():
            r = pool.execute("local", path, {}, {},
                             worker_type="subprocess", max_workers=1,
                             timeout=15)
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
        for r in results:
            self.assertEqual(r["done"], True)
        self.assertGreater(elapsed, 0.55, "Calls ran in parallel — semaphore broken")

    def test_persistent_worker_created_and_reused(self):
        from engine._pool import WorkerPool
        path = _temp_step("""
            import os
            def run(pd, **p):
                pd["pid"] = os.getpid()
                return pd
        """)
        pool = WorkerPool(idle_timeout=60)
        r1 = pool.execute("local", path, {}, {},
                          worker_type="persistent", timeout=10)
        r2 = pool.execute("local", path, {}, {},
                          worker_type="persistent", timeout=10)
        self.assertEqual(r1["pid"], r2["pid"])
        self.assertEqual(len(pool.active_workers()), 1)
        pool.shutdown_all()
        self.assertEqual(len(pool.active_workers()), 0)

    def test_reaper_removes_idle_worker(self):
        from engine._pool import WorkerPool
        path = _temp_step("def run(pd, **p): return pd")
        pool = WorkerPool(idle_timeout=0.2)
        pool.execute("local", path, {}, {},
                     worker_type="persistent", timeout=10)
        self.assertEqual(len(pool.active_workers()), 1)
        time.sleep(0.4)
        pool._reap_idle()
        self.assertEqual(len(pool.active_workers()), 0)
        pool.shutdown_all()

    def test_reaper_skips_active_worker(self):
        from engine._pool import WorkerPool
        path = _temp_step("""
            import time
            def run(pd, **p):
                time.sleep(0.5)
                return pd
        """)
        pool = WorkerPool(idle_timeout=0.1)
        result = [None]
        def run_step():
            result[0] = pool.execute("local", path, {}, {},
                                     worker_type="persistent", timeout=10)
        t = threading.Thread(target=run_step)
        t.start()
        time.sleep(0.2)
        pool._reap_idle()
        self.assertEqual(len(pool.active_workers()), 1)
        t.join(timeout=10)
        self.assertIsNotNone(result[0])
        pool.shutdown_all()

    def test_error_through_pool_has_remote_traceback(self):
        from engine._pool import WorkerPool
        path = _temp_step('def run(pd, **p): raise ValueError("pool err")')
        pool = WorkerPool()
        with self.assertRaises(StepExecutionError) as ctx:
            pool.execute("local", path, {}, {},
                         worker_type="subprocess", timeout=10)
        self.assertIn("pool err", str(ctx.exception))
        self.assertIn("ValueError", ctx.exception.remote_traceback)
        pool.shutdown_all()


# ── Pipeline Engine ───────────────────────────────────────────


class TestPipelineEngine(unittest.TestCase):

    def test_local_pipeline(self):
        from engine import PipelineEngine
        with PipelineEngine() as e:
            r = e.run_pipeline(
                str(PIPELINES_DIR / "test_local_pipeline.yaml"), "t", {},
            )
        self.assertIn("step_local", r)
        self.assertEqual(r["metadata"]["label"], "t")

    def test_data_flows_between_steps(self):
        _temp_step("""
            def run(pd, **p):
                pd["s1"] = "from_step_1"
                return pd
        """, name="flow_a")
        _temp_step("""
            def run(pd, **p):
                pd["s2_saw"] = pd.get("s1")
                return pd
        """, name="flow_b")
        yaml = _temp_yaml("wf:\n  - flow_a:\n  - flow_b:")
        from engine import PipelineEngine
        with PipelineEngine() as e:
            r = e.run_pipeline(yaml, "t", {})
        self.assertEqual(r["s2_saw"], "from_step_1")

    def test_step_returning_new_dict_replaces_data(self):
        """The return value, not in-place mutations, flows to the next step."""
        _temp_step("""
            def run(pd, **p):
                pd["mutated"] = True
                return {"returned": True}
        """, name="replace_a")
        _temp_step("""
            def run(pd, **p):
                pd["saw_mutated"] = pd.get("mutated")
                pd["saw_returned"] = pd.get("returned")
                return pd
        """, name="replace_b")
        yaml = _temp_yaml("wf:\n  - replace_a:\n  - replace_b:")
        from engine import PipelineEngine
        with PipelineEngine() as e:
            r = e.run_pipeline(yaml, "t", {})
        self.assertIsNone(r["saw_mutated"])
        self.assertEqual(r["saw_returned"], True)

    def test_error_in_step_raises_runtime_error(self):
        from engine import PipelineEngine
        with PipelineEngine() as e:
            with self.assertRaises(RuntimeError):
                e.run_pipeline(
                    str(PIPELINES_DIR / "test_error_pipeline.yaml"), "t", {},
                )

    def test_missing_step_raises(self):
        from engine import PipelineEngine
        with PipelineEngine() as e:
            with self.assertRaises(FileNotFoundError):
                e.run_pipeline(
                    str(PIPELINES_DIR / "test_missing_step_pipeline.yaml"),
                    "t", {},
                )

    def test_step_missing_run_raises_attribute_error(self):
        """A step with no run() function should fail clearly."""
        _temp_step('x = 1', name="no_run")
        yaml = _temp_yaml("wf:\n  - no_run:")
        from engine import PipelineEngine
        with PipelineEngine() as e:
            with self.assertRaises(AttributeError):
                e.run_pipeline(yaml, "t", {})

    def test_submit_returns_future(self):
        from engine import PipelineEngine
        with PipelineEngine() as e:
            f = e.submit(
                str(PIPELINES_DIR / "test_local_pipeline.yaml"), "t", {},
            )
            r = f.result(timeout=30)
        self.assertIn("step_local", r)

    def test_submit_callback(self):
        from engine import PipelineEngine
        called = []
        def cb(future):
            called.append(future.result())
        with PipelineEngine() as e:
            f = e.submit(
                str(PIPELINES_DIR / "test_local_pipeline.yaml"),
                "t", {}, callback=cb,
            )
            f.result(timeout=30)
        time.sleep(0.1)
        self.assertEqual(len(called), 1)
        self.assertIn("step_local", called[0])

    def test_custom_metadata_in_result(self):
        _temp_step("def run(pd, **p): return pd", name="meta_step")
        yaml = _temp_yaml("""
            metadata:
              purpose: "testing"
              author: "test"
            wf:
              - meta_step:
        """)
        from engine import PipelineEngine
        with PipelineEngine() as e:
            r = e.run_pipeline(yaml, "t", {})
        self.assertEqual(r["metadata"]["purpose"], "testing")
        self.assertEqual(r["metadata"]["author"], "test")


# ── Edge Cases: Return Values ─────────────────────────────────


class TestReturnValidation(unittest.TestCase):

    def test_none_return_raises_type_error(self):
        _temp_step('def run(pd, **p): return None', name="ret_none")
        yaml = _temp_yaml("wf:\n  - ret_none:")
        from engine import PipelineEngine
        with PipelineEngine() as e:
            with self.assertRaises(TypeError) as ctx:
                e.run_pipeline(yaml, "t", {})
        self.assertIn("ret_none", str(ctx.exception))
        self.assertIn("NoneType", str(ctx.exception))

    def test_list_return_raises_type_error(self):
        _temp_step('def run(pd, **p): return [1,2]', name="ret_list")
        yaml = _temp_yaml("wf:\n  - ret_list:")
        from engine import PipelineEngine
        with PipelineEngine() as e:
            with self.assertRaises(TypeError) as ctx:
                e.run_pipeline(yaml, "t", {})
        self.assertIn("list", str(ctx.exception))


# ── Edge Cases: YAML ──────────────────────────────────────────


class TestYAMLEdgeCases(unittest.TestCase):

    def test_empty_steps_raises(self):
        yaml = _temp_yaml("wf:")
        from engine import PipelineEngine
        with PipelineEngine() as e:
            with self.assertRaises(ValueError):
                e.run_pipeline(yaml, "t", {})

    def test_no_workflow_key_raises(self):
        yaml = _temp_yaml("metadata:\n  verbose: 0")
        from engine import PipelineEngine
        with PipelineEngine() as e:
            with self.assertRaises(ValueError):
                e.run_pipeline(yaml, "t", {})

    def test_null_params(self):
        _temp_step("def run(pd, **p):\n    pd['params'] = p\n    return pd",
                   name="null_params")
        yaml = _temp_yaml("wf:\n  - null_params:")
        from engine import PipelineEngine
        with PipelineEngine() as e:
            r = e.run_pipeline(yaml, "t", {})
        self.assertEqual(r["params"], {})

    def test_nonexistent_yaml_raises(self):
        from engine import run_pipeline
        with self.assertRaises(FileNotFoundError):
            run_pipeline("nonexistent.yaml", "t", {})

    def test_malformed_yaml_raises(self):
        import yaml as pyyaml
        path = Path(_TEMP_DIR) / f"bad_{_next_id()}.yaml"
        path.write_text("wf:\n  - : :\n:::")
        from engine import PipelineEngine
        with PipelineEngine() as e:
            with self.assertRaises(pyyaml.YAMLError):
                e.run_pipeline(str(path), "t", {})


# ── Edge Cases: Input Data ────────────────────────────────────


class TestInputData(unittest.TestCase):

    def test_none_becomes_empty_dict(self):
        from engine import run_pipeline
        r = run_pipeline(
            str(PIPELINES_DIR / "test_local_pipeline.yaml"), "t", None,
        )
        self.assertEqual(r["input"], {})

    def test_falsy_values_preserved(self):
        from engine import run_pipeline
        for val in ([], 0, False):
            r = run_pipeline(
                str(PIPELINES_DIR / "test_local_pipeline.yaml"), "t", val,
            )
            self.assertEqual(r["input"], val, f"Failed for {val!r}")


# ── Edge Cases: Lifecycle ─────────────────────────────────────


class TestLifecycle(unittest.TestCase):

    def test_run_after_shutdown_raises(self):
        from engine import PipelineEngine
        e = PipelineEngine()
        e.shutdown()
        with self.assertRaises(RuntimeError):
            e.run_pipeline(
                str(PIPELINES_DIR / "test_local_pipeline.yaml"), "t", {},
            )

    def test_submit_after_shutdown_raises(self):
        from engine import PipelineEngine
        e = PipelineEngine()
        e.shutdown()
        with self.assertRaises(RuntimeError):
            e.submit(
                str(PIPELINES_DIR / "test_local_pipeline.yaml"), "t", {},
            )

    def test_double_shutdown_safe(self):
        from engine import PipelineEngine
        e = PipelineEngine()
        e.shutdown()
        e.shutdown()
        self.assertFalse(e._accepting)

    def test_context_manager(self):
        from engine import PipelineEngine
        with PipelineEngine() as e:
            self.assertTrue(e._accepting)
        self.assertFalse(e._accepting)


# ── Multi-Job (adaptive feedback patterns) ────────────────────


class TestMultiJob(unittest.TestCase):
    """Multi-job tests simulating adaptive feedback microscopy workflows."""

    def test_concurrent_independent_jobs(self):
        """Submit multiple timepoints concurrently; verify data isolation."""
        _temp_step("""
            def run(pd, **p):
                t = pd["input"]["timepoint"]
                pd[f"result_t{t}"] = {"timepoint": t, "processed": True}
                return pd
        """, name="process_tp")
        yaml = _temp_yaml("wf:\n  - process_tp:")

        from engine import PipelineEngine
        with PipelineEngine() as engine:
            futures = [
                engine.submit(yaml, f"t{t}", {"timepoint": t})
                for t in range(5)
            ]
            results = [f.result(timeout=30) for f in futures]

        for t, r in enumerate(results):
            self.assertEqual(r[f"result_t{t}"]["timepoint"], t)
            self.assertEqual(r["metadata"]["label"], f"t{t}")

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
        """, name="adaptive_step")
        yaml = _temp_yaml("wf:\n  - adaptive_step:")

        from engine import PipelineEngine
        with PipelineEngine() as engine:
            feedback = None
            for t in range(3):
                r = engine.run_pipeline(
                    yaml, f"t{t}",
                    {"timepoint": t, "feedback": feedback},
                )
                feedback = r["result"]

        self.assertEqual(r["result"]["timepoint"], 2)
        self.assertTrue(r["result"]["used_feedback"])
        self.assertEqual(r["result"]["feedback_value"]["timepoint"], 1)

    def test_engine_recovers_after_failed_job(self):
        """A failed job doesn't prevent subsequent jobs from running."""
        _temp_step('def run(pd, **p): raise RuntimeError("bad")',
                   name="failing_step")
        _temp_step('def run(pd, **p): pd["ok"] = True; return pd',
                   name="good_step")
        bad_yaml = _temp_yaml("wf:\n  - failing_step:")
        good_yaml = _temp_yaml("wf:\n  - good_step:")

        from engine import PipelineEngine
        with PipelineEngine() as engine:
            with self.assertRaises(RuntimeError):
                engine.run_pipeline(bad_yaml, "bad", {})
            r = engine.run_pipeline(good_yaml, "good", {})
            self.assertTrue(r["ok"])

    def test_concurrent_mixed_success_and_failure(self):
        """Some jobs fail, others succeed; all futures resolve correctly."""
        _temp_step('def run(pd, **p): raise ValueError("fail")',
                   name="will_fail")
        _temp_step('def run(pd, **p): pd["ok"] = True; return pd',
                   name="will_pass")
        fail_yaml = _temp_yaml("wf:\n  - will_fail:")
        pass_yaml = _temp_yaml("wf:\n  - will_pass:")

        from engine import PipelineEngine
        with PipelineEngine() as engine:
            f_fail = engine.submit(fail_yaml, "fail", {})
            f_pass = engine.submit(pass_yaml, "pass", {})

            with self.assertRaises(ValueError):
                f_fail.result(timeout=30)
            r = f_pass.result(timeout=30)
            self.assertTrue(r["ok"])

    def test_many_concurrent_jobs(self):
        """Stress test: 20 concurrent submissions."""
        _temp_step("""
            def run(pd, **p):
                pd["job_id"] = pd["input"]["job_id"]
                return pd
        """, name="stress_step")
        yaml = _temp_yaml("wf:\n  - stress_step:")

        from engine import PipelineEngine
        n_jobs = 20
        with PipelineEngine(max_concurrent=n_jobs) as engine:
            futures = [
                engine.submit(yaml, f"j{i}", {"job_id": i})
                for i in range(n_jobs)
            ]
            results = [f.result(timeout=30) for f in futures]

        job_ids = sorted(r["job_id"] for r in results)
        self.assertEqual(job_ids, list(range(n_jobs)))

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
        """, name="accumulate")
        yaml = _temp_yaml("wf:\n  - accumulate:")

        from engine import PipelineEngine
        history = []
        with PipelineEngine() as engine:
            for t in range(4):
                r = engine.run_pipeline(
                    yaml, f"t{t}",
                    {"timepoint": t, "history": history},
                )
                history.append(r["result"])

        self.assertEqual(len(history), 4)
        self.assertEqual(history[0]["history_length"], 0)
        self.assertEqual(history[3]["history_length"], 3)
        self.assertEqual(history[3]["cells_found"], 35)

    def test_concurrent_different_pipelines(self):
        """Different pipeline configs submitted concurrently."""
        _temp_step('def run(pd, **p): pd["a"] = 1; return pd',
                   name="pipe_a_step")
        _temp_step('def run(pd, **p): pd["b"] = 2; return pd',
                   name="pipe_b_step")
        yaml_a = _temp_yaml("wf:\n  - pipe_a_step:")
        yaml_b = _temp_yaml("wf:\n  - pipe_b_step:")

        from engine import PipelineEngine
        with PipelineEngine() as engine:
            fa = engine.submit(yaml_a, "a", {})
            fb = engine.submit(yaml_b, "b", {})
            ra = fa.result(timeout=30)
            rb = fb.result(timeout=30)

        self.assertEqual(ra["a"], 1)
        self.assertNotIn("b", ra)
        self.assertEqual(rb["b"], 2)
        self.assertNotIn("a", rb)

    def test_callbacks_fire_for_all_concurrent_jobs(self):
        """Every concurrent job's callback fires."""
        _temp_step("def run(pd, **p): return pd", name="cb_step")
        yaml = _temp_yaml("wf:\n  - cb_step:")

        from engine import PipelineEngine
        labels = []
        lock = threading.Lock()

        def on_done(future):
            with lock:
                labels.append(future.result()["metadata"]["label"])

        with PipelineEngine() as engine:
            futures = [
                engine.submit(yaml, f"j{i}", {}, callback=on_done)
                for i in range(5)
            ]
            for f in futures:
                f.result(timeout=30)

        time.sleep(0.2)
        self.assertEqual(sorted(labels), [f"j{i}" for i in range(5)])

    def test_concurrent_multi_step_jobs(self):
        """Multiple multi-step pipelines submitted concurrently."""
        _temp_step("""
            def run(pd, **p):
                pd["step1"] = pd["input"]["job_id"]
                return pd
        """, name="multi_s1")
        _temp_step("""
            def run(pd, **p):
                pd["step2"] = pd["step1"] * 2
                return pd
        """, name="multi_s2")
        _temp_step("""
            def run(pd, **p):
                pd["final"] = pd["step2"] + 1
                return pd
        """, name="multi_s3")
        yaml = _temp_yaml("wf:\n  - multi_s1:\n  - multi_s2:\n  - multi_s3:")

        from engine import PipelineEngine
        with PipelineEngine() as engine:
            futures = [
                engine.submit(yaml, f"j{i}", {"job_id": i})
                for i in range(5)
            ]
            results = [f.result(timeout=30) for f in futures]

        for i, r in enumerate(results):
            self.assertEqual(r["step1"], i)
            self.assertEqual(r["step2"], i * 2)
            self.assertEqual(r["final"], i * 2 + 1)

    def test_successive_batches(self):
        """Engine processes multiple waves of concurrent jobs."""
        _temp_step("""
            def run(pd, **p):
                pd["batch"] = pd["input"]["batch"]
                pd["idx"] = pd["input"]["idx"]
                return pd
        """, name="batch_step")
        yaml = _temp_yaml("wf:\n  - batch_step:")

        from engine import PipelineEngine
        with PipelineEngine() as engine:
            for batch in range(3):
                futures = [
                    engine.submit(yaml, f"b{batch}_j{i}",
                                  {"batch": batch, "idx": i})
                    for i in range(4)
                ]
                results = [f.result(timeout=30) for f in futures]

                for i, r in enumerate(results):
                    self.assertEqual(r["batch"], batch)
                    self.assertEqual(r["idx"], i)

    def test_interleaved_different_yamls(self):
        """Different pipeline configs submitted in interleaved order."""
        _temp_step('def run(pd, **p): pd["type"] = "A"; return pd',
                   name="type_a")
        _temp_step('def run(pd, **p): pd["type"] = "B"; return pd',
                   name="type_b")
        _temp_step('def run(pd, **p): pd["type"] = "C"; return pd',
                   name="type_c")
        yaml_a = _temp_yaml("wf:\n  - type_a:")
        yaml_b = _temp_yaml("wf:\n  - type_b:")
        yaml_c = _temp_yaml("wf:\n  - type_c:")

        from engine import PipelineEngine
        with PipelineEngine() as engine:
            futures = []
            for i in range(4):
                futures.append(("A", engine.submit(yaml_a, f"a{i}", {})))
                futures.append(("B", engine.submit(yaml_b, f"b{i}", {})))
                futures.append(("C", engine.submit(yaml_c, f"c{i}", {})))

            for expected, f in futures:
                r = f.result(timeout=30)
                self.assertEqual(r["type"], expected)


# ── Isolation (real subprocess execution) ─────────────────────


@unittest.skipUnless(_ENV_B_EXISTS, f"Requires {_TEST_ENV_B}")
class TestIsolation(unittest.TestCase):
    """Pipeline-level isolation tests with real conda environments."""

    def test_single_isolated_step(self):
        """One step routed to an isolated environment."""
        _temp_step(f"""
            METADATA = {{"environment": "{_TEST_ENV_B}"}}
            def run(pd, **p):
                import sys, os
                pd["env"] = os.path.basename(sys.prefix)
                pd["pid"] = os.getpid()
                return pd
        """, name="iso_single")
        yaml = _temp_yaml("wf:\n  - iso_single:")

        from engine import PipelineEngine
        with PipelineEngine() as engine:
            r = engine.run_pipeline(yaml, "t", {})
        self.assertEqual(r["env"], _TEST_ENV_B)
        self.assertNotEqual(r["pid"], os.getpid())

    def test_local_to_isolated_to_local(self):
        """Data flows through local -> isolated -> local."""
        _temp_step("""
            def run(pd, **p):
                pd["from_local_1"] = 42
                return pd
        """, name="iso_l1")
        _temp_step(f"""
            METADATA = {{"environment": "{_TEST_ENV_B}"}}
            def run(pd, **p):
                pd["from_isolated"] = pd["from_local_1"] * 2
                return pd
        """, name="iso_m")
        _temp_step("""
            def run(pd, **p):
                pd["from_local_2"] = pd["from_isolated"] + 1
                return pd
        """, name="iso_l2")
        yaml = _temp_yaml("wf:\n  - iso_l1:\n  - iso_m:\n  - iso_l2:")

        from engine import PipelineEngine
        with PipelineEngine() as engine:
            r = engine.run_pipeline(yaml, "t", {})
        self.assertEqual(r["from_local_1"], 42)
        self.assertEqual(r["from_isolated"], 84)
        self.assertEqual(r["from_local_2"], 85)

    @unittest.skipUnless(_ENV_C_EXISTS, f"Also requires {_TEST_ENV_C}")
    def test_two_different_isolated_envs(self):
        """Steps in two different isolated environments."""
        _temp_step(f"""
            METADATA = {{"environment": "{_TEST_ENV_B}"}}
            def run(pd, **p):
                import sys, os
                pd["env_b"] = os.path.basename(sys.prefix)
                pd["from_b"] = 10
                return pd
        """, name="iso_env_b")
        _temp_step(f"""
            METADATA = {{"environment": "{_TEST_ENV_C}"}}
            def run(pd, **p):
                import sys, os
                pd["env_c"] = os.path.basename(sys.prefix)
                pd["from_c"] = pd["from_b"] + 5
                return pd
        """, name="iso_env_c")
        yaml = _temp_yaml("wf:\n  - iso_env_b:\n  - iso_env_c:")

        from engine import PipelineEngine
        with PipelineEngine() as engine:
            r = engine.run_pipeline(yaml, "t", {})
        self.assertEqual(r["env_b"], _TEST_ENV_B)
        self.assertEqual(r["env_c"], _TEST_ENV_C)
        self.assertEqual(r["from_c"], 15)

    def test_pipeline_env_override(self):
        """Pipeline-level environment forces isolation for local steps."""
        _temp_step("""
            def run(pd, **p):
                import sys, os
                pd["env"] = os.path.basename(sys.prefix)
                return pd
        """, name="iso_override")
        yaml = _temp_yaml(f"""
            metadata:
              environment: "{_TEST_ENV_B}"
            wf:
              - iso_override:
        """)

        from engine import PipelineEngine
        with PipelineEngine() as engine:
            r = engine.run_pipeline(yaml, "t", {})
        self.assertEqual(r["env"], _TEST_ENV_B)

    def test_persistent_worker_step(self):
        """Persistent worker keeps process alive between pipeline runs."""
        _temp_step(f"""
            METADATA = {{
                "environment": "{_TEST_ENV_B}",
                "worker": "persistent",
            }}
            def run(pd, **p):
                import os
                pd["pid"] = os.getpid()
                return pd
        """, name="iso_persist")
        yaml = _temp_yaml("wf:\n  - iso_persist:")

        from engine import PipelineEngine
        with PipelineEngine() as engine:
            r1 = engine.run_pipeline(yaml, "t1", {})
            r2 = engine.run_pipeline(yaml, "t2", {})
        self.assertEqual(r1["pid"], r2["pid"])

    def test_complex_data_survives_isolation(self):
        """Complex Python types survive serialization across environments."""
        _temp_step(f"""
            METADATA = {{"environment": "{_TEST_ENV_B}"}}
            def run(pd, **p):
                pd["echo"] = pd["input"]
                return pd
        """, name="iso_echo")
        yaml = _temp_yaml("wf:\n  - iso_echo:")

        data = {
            "tuple": (1, 2, 3),
            "set": {4, 5, 6},
            "bytes": b"\x00\xff",
            "nested": {"a": [None, True, {"b": 2.5}]},
            "large_int": 10**50,
        }
        from engine import PipelineEngine
        with PipelineEngine() as engine:
            r = engine.run_pipeline(yaml, "t", data)
        self.assertEqual(r["echo"]["tuple"], (1, 2, 3))
        self.assertEqual(r["echo"]["set"], {4, 5, 6})
        self.assertEqual(r["echo"]["bytes"], b"\x00\xff")
        self.assertEqual(r["echo"]["nested"]["a"][2]["b"], 2.5)
        self.assertEqual(r["echo"]["large_int"], 10**50)

    def test_step_error_in_isolated_env(self):
        """Errors in isolated steps become StepExecutionError with traceback."""
        _temp_step(f"""
            METADATA = {{"environment": "{_TEST_ENV_B}"}}
            def run(pd, **p): raise ValueError("isolated error")
        """, name="iso_err")
        yaml = _temp_yaml("wf:\n  - iso_err:")

        from engine._errors import StepExecutionError
        from engine import PipelineEngine
        with PipelineEngine() as engine:
            with self.assertRaises(StepExecutionError) as ctx:
                engine.run_pipeline(yaml, "t", {})
        self.assertIn("isolated error", str(ctx.exception))
        self.assertIn("ValueError", ctx.exception.remote_traceback)

    def test_return_validation_through_isolation(self):
        """Non-dict return from isolated step raises TypeError."""
        _temp_step(f"""
            METADATA = {{"environment": "{_TEST_ENV_B}"}}
            def run(pd, **p): return ["not", "a", "dict"]
        """, name="iso_bad_ret")
        yaml = _temp_yaml("wf:\n  - iso_bad_ret:")

        from engine import PipelineEngine
        with PipelineEngine() as engine:
            with self.assertRaises(TypeError):
                engine.run_pipeline(yaml, "t", {})


# ── Multi-Job Isolation ──────────────────────────────────────


@unittest.skipUnless(_ENV_B_EXISTS, f"Requires {_TEST_ENV_B}")
class TestMultiJobIsolation(unittest.TestCase):
    """Multi-job tests with real environment isolation."""

    def test_concurrent_isolated_jobs(self):
        """Multiple isolated jobs submitted concurrently."""
        _temp_step(f"""
            METADATA = {{"environment": "{_TEST_ENV_B}"}}
            def run(pd, **p):
                import os
                pd["job_id"] = pd["input"]["job_id"]
                pd["pid"] = os.getpid()
                return pd
        """, name="mji_concurrent")
        yaml = _temp_yaml("wf:\n  - mji_concurrent:")

        from engine import PipelineEngine
        with PipelineEngine() as engine:
            futures = [
                engine.submit(yaml, f"j{i}", {"job_id": i})
                for i in range(3)
            ]
            results = [f.result(timeout=60) for f in futures]

        for i, r in enumerate(results):
            self.assertEqual(r["job_id"], i)

    def test_concurrent_mixed_local_and_isolated(self):
        """Local and isolated jobs submitted concurrently."""
        _temp_step("""
            def run(pd, **p):
                pd["mode"] = "local"
                return pd
        """, name="mji_local")
        _temp_step(f"""
            METADATA = {{"environment": "{_TEST_ENV_B}"}}
            def run(pd, **p):
                import sys, os
                pd["mode"] = "isolated"
                pd["env"] = os.path.basename(sys.prefix)
                return pd
        """, name="mji_iso")
        yaml_l = _temp_yaml("wf:\n  - mji_local:")
        yaml_i = _temp_yaml("wf:\n  - mji_iso:")

        from engine import PipelineEngine
        with PipelineEngine() as engine:
            futures = []
            for i in range(3):
                futures.append(("local", engine.submit(yaml_l, f"l{i}", {})))
                futures.append(("isolated", engine.submit(yaml_i, f"i{i}", {})))

            for expected, f in futures:
                r = f.result(timeout=60)
                self.assertEqual(r["mode"], expected)
                if expected == "isolated":
                    self.assertEqual(r["env"], _TEST_ENV_B)

    def test_sequential_adaptive_with_isolation(self):
        """Adaptive feedback loop with isolated processing step."""
        _temp_step("""
            def run(pd, **p):
                t = pd["input"]["timepoint"]
                pd["acquired"] = {"t": t, "data": list(range(t + 1))}
                return pd
        """, name="mji_acquire")
        _temp_step(f"""
            METADATA = {{"environment": "{_TEST_ENV_B}"}}
            def run(pd, **p):
                pd["processed"] = {{
                    "n": len(pd["acquired"]["data"]),
                    "t": pd["acquired"]["t"],
                }}
                return pd
        """, name="mji_process")
        yaml = _temp_yaml("wf:\n  - mji_acquire:\n  - mji_process:")

        from engine import PipelineEngine
        with PipelineEngine() as engine:
            for t in range(3):
                r = engine.run_pipeline(yaml, f"t{t}", {"timepoint": t})
                self.assertEqual(r["processed"]["t"], t)
                self.assertEqual(r["processed"]["n"], t + 1)

    def test_multi_step_mixed_isolation_concurrent(self):
        """Multi-step pipelines (local+isolated) submitted concurrently."""
        _temp_step("""
            def run(pd, **p):
                pd["local_1"] = pd["input"]["job_id"]
                return pd
        """, name="mji_ms_l1")
        _temp_step(f"""
            METADATA = {{"environment": "{_TEST_ENV_B}"}}
            def run(pd, **p):
                pd["isolated"] = pd["local_1"] * 10
                return pd
        """, name="mji_ms_iso")
        _temp_step("""
            def run(pd, **p):
                pd["local_2"] = pd["isolated"] + 1
                return pd
        """, name="mji_ms_l2")
        yaml = _temp_yaml("""
            wf:
              - mji_ms_l1:
              - mji_ms_iso:
              - mji_ms_l2:
        """)

        from engine import PipelineEngine
        with PipelineEngine() as engine:
            futures = [
                engine.submit(yaml, f"j{i}", {"job_id": i})
                for i in range(3)
            ]
            results = [f.result(timeout=60) for f in futures]

        for i, r in enumerate(results):
            self.assertEqual(r["local_1"], i)
            self.assertEqual(r["isolated"], i * 10)
            self.assertEqual(r["local_2"], i * 10 + 1)

    def test_persistent_worker_reused_across_jobs(self):
        """Persistent worker reused across sequential pipeline runs."""
        _temp_step(f"""
            METADATA = {{
                "environment": "{_TEST_ENV_B}",
                "worker": "persistent",
            }}
            def run(pd, **p):
                import os
                pd["pid"] = os.getpid()
                pd["job_id"] = pd["input"]["job_id"]
                return pd
        """, name="mji_persist")
        yaml = _temp_yaml("wf:\n  - mji_persist:")

        from engine import PipelineEngine
        with PipelineEngine() as engine:
            results = []
            for i in range(3):
                r = engine.run_pipeline(yaml, f"j{i}", {"job_id": i})
                results.append(r)

        pids = {r["pid"] for r in results}
        self.assertEqual(len(pids), 1, "Persistent worker should be reused")
        for i, r in enumerate(results):
            self.assertEqual(r["job_id"], i)

    def test_interleaved_yamls_mixed_isolation(self):
        """Interleaved local and isolated pipeline submissions."""
        _temp_step("""
            def run(pd, **p):
                pd["type"] = "local"
                return pd
        """, name="mji_il_local")
        _temp_step(f"""
            METADATA = {{"environment": "{_TEST_ENV_B}"}}
            def run(pd, **p):
                pd["type"] = "isolated"
                return pd
        """, name="mji_il_iso")
        yaml_l = _temp_yaml("wf:\n  - mji_il_local:")
        yaml_i = _temp_yaml("wf:\n  - mji_il_iso:")

        from engine import PipelineEngine
        with PipelineEngine() as engine:
            futures = []
            for i in range(4):
                futures.append(("local", engine.submit(yaml_l, f"l{i}", {})))
                futures.append(("isolated", engine.submit(yaml_i, f"i{i}", {})))

            for expected, f in futures:
                r = f.result(timeout=60)
                self.assertEqual(r["type"], expected)


# ── Regression ────────────────────────────────────────────────
#
# Each test guards against a specific past bug.
# Docstrings explain what broke and how it was fixed.


class TestRegression(unittest.TestCase):
    """
    Regression tests for known past bugs.

    These must never be removed — they exist because each failure mode
    actually happened or was discovered during review. If a refactor
    breaks one, the original bug has been reintroduced.
    """

    def test_metadata_extraction_never_executes_code(self):
        """
        Bug: get_step_settings() used exec() to read METADATA, running all
        module-level imports. A step with 'import torch' at module level
        crashed the main process even when destined for a subprocess.

        Fix: AST-based extraction in _loader._extract_metadata().
        """
        path = _temp_step("""
            import nonexistent_package_that_would_crash
            raise RuntimeError("this line must never execute")
            METADATA = {"environment": "some_remote_env", "worker": "persistent"}
            def run(pd, **p): return pd
        """)
        # Must succeed — no code is executed, only the AST is parsed
        s = get_step_settings(Path(path))
        self.assertEqual(s["environment"], "some_remote_env")
        self.assertEqual(s["worker"], "persistent")

    @unittest.skipUnless(_ENV_B_EXISTS, f"Requires {_TEST_ENV_B}")
    def test_load_function_only_called_for_local_steps(self):
        """
        Bug: _pipeline.py called load_function() for EVERY step, even
        isolated ones. The module was exec'd in the main process then
        discarded — wasteful and dangerous (wrong-env imports crash).

        Fix: load_function() moved into the else branch (local-only).
        """
        _temp_step("""
            def run(pd, **p):
                pd["local"] = True
                return pd
        """, name="reg_local")
        _temp_step(f"""
            METADATA = {{"environment": "{_TEST_ENV_B}"}}
            def run(pd, **p):
                pd["isolated"] = True
                return pd
        """, name="reg_isolated")
        yaml = _temp_yaml("wf:\n  - reg_local:\n  - reg_isolated:")

        from unittest.mock import patch
        from engine import _pipeline, PipelineEngine
        original = _pipeline.load_function
        loaded_names = []

        def tracking_load(name, *args, **kwargs):
            loaded_names.append(name)
            return original(name, *args, **kwargs)

        with patch.object(_pipeline, 'load_function',
                          side_effect=tracking_load):
            with PipelineEngine() as engine:
                r = engine.run_pipeline(yaml, "t", {})

        self.assertTrue(r["local"])
        self.assertTrue(r["isolated"])
        self.assertEqual(loaded_names, ["reg_local"],
                         "load_function must only run for local steps, "
                         f"but ran for: {loaded_names}")

    def test_pool_semaphore_uses_local_reference(self):
        """
        Bug: pool.execute() accessed self._semaphores[key] outside the pool
        lock at .acquire() and .release(). If the reaper or shutdown_all()
        popped the key concurrently, KeyError was raised.

        Fix: local reference (sem = ...) taken inside the lock.
        """
        import inspect
        from engine._pool import WorkerPool
        source = inspect.getsource(WorkerPool.execute)
        self.assertNotIn(
            "self._semaphores[key].acquire", source,
            "Semaphore dict lookup outside lock — race with reaper/shutdown"
        )
        self.assertNotIn(
            "self._semaphores[key].release", source,
            "Semaphore dict lookup outside lock — race with reaper/shutdown"
        )

    def test_local_error_preserves_original_exception_type(self):
        """
        Contract: local steps propagate the original exception type.
        Code that catches only StepExecutionError would silently miss
        errors from local steps.
        """
        _temp_step('def run(pd, **p): raise ValueError("local boom")',
                   name="reg_local_err")
        yaml = _temp_yaml("wf:\n  - reg_local_err:")

        from engine import PipelineEngine
        with PipelineEngine() as engine:
            with self.assertRaises(ValueError) as ctx:
                engine.run_pipeline(yaml, "t", {})
        self.assertIn("local boom", str(ctx.exception))

    @unittest.skipUnless(_ENV_B_EXISTS, f"Requires {_TEST_ENV_B}")
    def test_isolated_error_wraps_in_step_execution_error(self):
        """
        Contract: isolated steps wrap errors in StepExecutionError with
        remote_traceback. Code that catches only ValueError would miss
        errors from isolated steps.
        """
        _temp_step(f"""
            METADATA = {{"environment": "{_TEST_ENV_B}"}}
            def run(pd, **p): raise ValueError("isolated boom")
        """, name="reg_iso_err")
        yaml = _temp_yaml("wf:\n  - reg_iso_err:")

        from engine._errors import StepExecutionError
        from engine import PipelineEngine
        with PipelineEngine() as engine:
            with self.assertRaises(StepExecutionError) as ctx:
                engine.run_pipeline(yaml, "t", {})
        self.assertIn("isolated boom", str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, ValueError)

    def test_step_without_metadata_defaults_to_local(self):
        """
        Bug: if AST extraction returns {} for a step with no METADATA,
        the defaults must be safe (local, subprocess, 1). A crash or
        routing to a nonexistent environment would break simple steps.
        """
        _temp_step("def run(pd, **p): pd['ok'] = True; return pd",
                   name="reg_bare")
        yaml = _temp_yaml("wf:\n  - reg_bare:")

        from engine import PipelineEngine
        with PipelineEngine() as engine:
            r = engine.run_pipeline(yaml, "t", {})
        self.assertTrue(r["ok"])

    def test_missing_run_fails_at_execution_not_routing(self):
        """
        Bug: with AST extraction, routing succeeds even if the step has
        no run() function. The error must come at execution time with a
        clear AttributeError, not during METADATA reading.
        """
        _temp_step("x = 1", name="reg_no_run")
        yaml = _temp_yaml("wf:\n  - reg_no_run:")

        from engine import PipelineEngine
        with PipelineEngine() as engine:
            with self.assertRaises(AttributeError):
                engine.run_pipeline(yaml, "t", {})

    @unittest.skipUnless(_ENV_B_EXISTS, f"Requires {_TEST_ENV_B}")
    def test_pipeline_data_keys_survive_env_crossing(self):
        """
        Bug: data accumulated by local steps could be lost during pickle
        serialization when crossing an environment boundary. All keys
        from prior steps must survive the round-trip intact.
        """
        _temp_step("""
            def run(pd, **p):
                pd["step_1"] = {"value": 42, "nested": [1, 2, 3]}
                return pd
        """, name="reg_surv_1")
        _temp_step(f"""
            METADATA = {{"environment": "{_TEST_ENV_B}"}}
            def run(pd, **p):
                pd["step_2"] = pd["step_1"]["value"] * 2
                return pd
        """, name="reg_surv_2")
        _temp_step("""
            def run(pd, **p):
                pd["step_3"] = {
                    "saw_step_1": "step_1" in pd,
                    "saw_step_2": "step_2" in pd,
                    "step_1_nested": pd.get("step_1", {}).get("nested"),
                }
                return pd
        """, name="reg_surv_3")
        yaml = _temp_yaml(
            "wf:\n  - reg_surv_1:\n  - reg_surv_2:\n  - reg_surv_3:"
        )

        from engine import PipelineEngine
        with PipelineEngine() as engine:
            r = engine.run_pipeline(yaml, "t", {})

        self.assertTrue(r["step_3"]["saw_step_1"])
        self.assertTrue(r["step_3"]["saw_step_2"])
        self.assertEqual(r["step_3"]["step_1_nested"], [1, 2, 3])
        self.assertEqual(r["step_2"], 84)

    def test_get_step_settings_returns_dict_not_module(self):
        """
        Bug: get_step_settings() used to take a module object. After the
        AST refactor it takes a file path. If someone accidentally reverts
        the signature, this test catches it immediately.
        """
        path = _temp_step('METADATA = {"environment": "test_env"}')
        # Must accept a Path, not a module
        s = get_step_settings(Path(path))
        self.assertEqual(s["environment"], "test_env")


# ── Package API ───────────────────────────────────────────────


class TestPackageAPI(unittest.TestCase):

    def test_public_imports(self):
        from engine import (run_pipeline, PipelineEngine,
                            WorkerError, WorkerSpawnError,
                            WorkerCrashedError, StepExecutionError)
        self.assertTrue(callable(run_pipeline))
        self.assertTrue(callable(PipelineEngine))
        self.assertTrue(issubclass(WorkerSpawnError, WorkerError))
        self.assertTrue(issubclass(WorkerCrashedError, WorkerError))
        self.assertTrue(issubclass(StepExecutionError, WorkerError))

    def test_version(self):
        import engine
        self.assertIsInstance(engine.__version__, str)


if __name__ == "__main__":
    unittest.main()
