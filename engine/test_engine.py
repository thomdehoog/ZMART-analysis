"""
Unit and integration tests for the unified pipeline engine.

Run from the engine directory:
    python test_engine.py
    python -m pytest test_engine.py -v
"""

import atexit
import shutil
import sys
import tempfile
import textwrap
import threading
import time
import types
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
        header = f'metadata:\n  functions_dir: "{_TEMP_DIR}"\n'
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

    def _module(self, **kw):
        mod = types.ModuleType("fake")
        if kw:
            mod.METADATA = kw
        return mod

    def test_defaults_no_metadata(self):
        """Module with no METADATA attribute gets all defaults."""
        s = get_step_settings(types.ModuleType("bare"))
        self.assertEqual(s, {"environment": "local", "worker": "subprocess",
                             "max_workers": 1})

    def test_explicit(self):
        s = get_step_settings(self._module(
            environment="gpu", worker="persistent", max_workers=2,
        ))
        self.assertEqual(s["environment"], "gpu")
        self.assertEqual(s["worker"], "persistent")
        self.assertEqual(s["max_workers"], 2)

    def test_partial(self):
        s = get_step_settings(self._module(environment="cpu"))
        self.assertEqual(s["environment"], "cpu")
        self.assertEqual(s["worker"], "subprocess")

    def test_no_data_transfer_in_output(self):
        self.assertNotIn("data_transfer", get_step_settings(self._module()))

    def test_metadata_none_uses_defaults(self):
        mod = types.ModuleType("bad")
        mod.METADATA = None
        s = get_step_settings(mod)
        self.assertEqual(s["environment"], "local")


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
