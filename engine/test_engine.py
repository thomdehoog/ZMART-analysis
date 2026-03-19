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


def _temp_step(code, name=None):
    """Write a temporary step .py file to _TEMP_DIR."""
    path = Path(_TEMP_DIR) / (f"{name}.py" if name else
                               f"step_{id(code)}.py")
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
    path = Path(_TEMP_DIR) / f"pipeline_{id(content)}.yaml"
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

    def test_loads_valid_step(self):
        module = load_function("step_local", STEPS_DIR)
        self.assertTrue(callable(module.run))

    def test_loads_metadata(self):
        module = load_function("step_local", STEPS_DIR)
        self.assertIsInstance(module.METADATA, dict)

    def test_module_name(self):
        module = load_function("step_local", STEPS_DIR)
        self.assertEqual(module.__name__, "step_local")

    def test_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            load_function("does_not_exist", STEPS_DIR)

    def test_step_with_import_error(self):
        path = _temp_step("""
            import nonexistent_xyz
            def run(pipeline_data, **params): return pipeline_data
        """)
        with self.assertRaises(ModuleNotFoundError):
            load_function(Path(path).stem, Path(path).parent)


class TestGetStepSettings(unittest.TestCase):

    def _module(self, **kw):
        mod = types.ModuleType("fake")
        if kw:
            mod.METADATA = kw
        return mod

    def test_defaults(self):
        s = get_step_settings(types.ModuleType("bare"))
        self.assertEqual(s["environment"], "local")
        self.assertEqual(s["worker"], "subprocess")
        self.assertEqual(s["max_workers"], 1)

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

    def test_no_data_transfer(self):
        """data_transfer was removed — should not appear in settings."""
        s = get_step_settings(self._module())
        self.assertNotIn("data_transfer", s)


# ── Worker (oneshot) ──────────────────────────────────────────


class TestWorkerOneshot(unittest.TestCase):
    """Test the Worker in oneshot mode (the unified subprocess path)."""

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
        self.assertTrue(result["ran"])
        self.assertEqual(result["x"], 42)
        self.assertEqual(result["input"], 1)

    def test_step_error_raises(self):
        from engine._worker import Worker
        path = _temp_step("""
            def run(pipeline_data, **params):
                raise ValueError("intentional")
        """)
        w = Worker("local", path, oneshot=True, connect_timeout=10)
        with self.assertRaises(StepExecutionError) as ctx:
            w.execute({}, {}, timeout=10)
        w.shutdown()
        self.assertIn("intentional", str(ctx.exception))
        self.assertIsNotNone(ctx.exception.remote_traceback)

    def test_preserves_complex_types(self):
        """Pickle over TCP preserves tuples, nested dicts, etc."""
        from engine._worker import Worker
        path = _temp_step("""
            def run(pipeline_data, **params):
                pipeline_data["type"] = type(pipeline_data["t"]).__name__
                return pipeline_data
        """)
        w = Worker("local", path, oneshot=True, connect_timeout=10)
        try:
            result = w.execute({"t": (1, 2, 3)}, {}, timeout=10)
        finally:
            w.shutdown()
        self.assertEqual(result["type"], "tuple")

    def test_process_exits_after_oneshot(self):
        from engine._worker import Worker
        path = _temp_step("""
            def run(pipeline_data, **params):
                return pipeline_data
        """)
        w = Worker("local", path, oneshot=True, connect_timeout=10)
        w.execute({}, {}, timeout=10)
        time.sleep(0.5)
        self.assertFalse(w.is_alive())
        w.shutdown()

    def test_shutdown_after_oneshot_is_safe(self):
        """Shutdown on an already-exited oneshot worker should not raise."""
        from engine._worker import Worker
        path = _temp_step("""
            def run(pipeline_data, **params):
                return pipeline_data
        """)
        w = Worker("local", path, oneshot=True, connect_timeout=10)
        w.execute({}, {}, timeout=10)
        time.sleep(0.3)
        w.shutdown()
        w.shutdown()  # double shutdown

    def test_oneshot_error_still_cleans_up(self):
        """Worker resources are released even when step raises."""
        from engine._worker import Worker
        path = _temp_step("""
            def run(pd, **p): raise RuntimeError("fail")
        """)
        w = Worker("local", path, oneshot=True, connect_timeout=10)
        with self.assertRaises(StepExecutionError):
            w.execute({}, {}, timeout=10)
        w.shutdown()
        self.assertIsNone(w._conn)
        self.assertIsNone(w._listener)
        self.assertIsNone(w._process)

    def test_oneshot_via_pool(self):
        """Pool's _execute_oneshot creates, uses, and discards a Worker."""
        from engine._pool import WorkerPool
        path = _temp_step("""
            def run(pd, **p):
                pd["pooled"] = True
                return pd
        """)
        pool = WorkerPool()
        result = pool.execute(
            "local", path, {}, {},
            worker_type="subprocess", timeout=10,
        )
        self.assertTrue(result["pooled"])
        self.assertEqual(pool.active_workers(), [])
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

    def test_data_flows_between_steps(self):
        from engine import PipelineEngine
        with PipelineEngine() as e:
            r = e.run_pipeline(
                str(PIPELINES_DIR / "test_mixed_pipeline.yaml"), "t", {},
            )
        self.assertIn("step_local", r)
        self.assertIn("step_local_2", r)

    def test_error_in_step_raises(self):
        from engine import PipelineEngine
        with PipelineEngine() as e:
            with self.assertRaises(Exception):
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

    def test_submit_returns_future(self):
        from engine import PipelineEngine
        with PipelineEngine() as e:
            f = e.submit(
                str(PIPELINES_DIR / "test_local_pipeline.yaml"), "t", {},
            )
            r = f.result(timeout=30)
        self.assertIn("step_local", r)

    def test_metadata_label(self):
        from engine import run_pipeline
        r = run_pipeline(
            str(PIPELINES_DIR / "test_local_pipeline.yaml"), "my_label", {},
        )
        self.assertEqual(r["metadata"]["label"], "my_label")


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

    def test_no_metadata_uses_defaults(self):
        _temp_step("""
            METADATA = {"environment": "local"}
            def run(pd, **p):
                pd["ok"] = True
                return pd
        """, name="bare_step")
        yaml = _temp_yaml("wf:\n  - bare_step:")
        from engine import PipelineEngine
        with PipelineEngine() as e:
            r = e.run_pipeline(yaml, "t", {})
        self.assertTrue(r["ok"])

    def test_null_params(self):
        _temp_step("""
            def run(pd, **p):
                pd["params"] = p
                return pd
        """, name="null_params")
        yaml = _temp_yaml("wf:\n  - null_params:")
        from engine import PipelineEngine
        with PipelineEngine() as e:
            r = e.run_pipeline(yaml, "t", {})
        self.assertEqual(r["params"], {})

    def test_nonexistent_yaml_raises(self):
        from engine import run_pipeline
        with self.assertRaises(FileNotFoundError):
            run_pipeline("nonexistent.yaml", "t", {})


# ── Edge Cases: Input Data ────────────────────────────────────


class TestInputData(unittest.TestCase):

    def test_none_becomes_empty_dict(self):
        from engine import run_pipeline
        r = run_pipeline(
            str(PIPELINES_DIR / "test_local_pipeline.yaml"), "t", None,
        )
        self.assertEqual(r["input"], {})

    def test_falsy_values_preserved(self):
        """Empty list, zero, False should NOT be replaced with {}."""
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

    def test_context_manager(self):
        from engine import PipelineEngine
        with PipelineEngine() as e:
            self.assertTrue(e._accepting)
        self.assertFalse(e._accepting)


# ── Edge Cases: Pool ──────────────────────────────────────────


class TestPoolEdgeCases(unittest.TestCase):

    def test_shutdown_before_use(self):
        from engine._pool import WorkerPool
        pool = WorkerPool()
        pool.shutdown_all()

    def test_empty_pool(self):
        from engine._pool import WorkerPool
        pool = WorkerPool()
        self.assertEqual(pool.active_workers(), [])
        pool.shutdown_all()

    def test_semaphore_limits_concurrency(self):
        from engine._pool import WorkerPool
        path = _temp_step("""
            import time
            def run(pd, **p):
                time.sleep(0.3)
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

        threads = [threading.Thread(target=run_one) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        pool.shutdown_all()

        self.assertEqual(len(results), 2)
        for r in results:
            self.assertTrue(r["done"])


# ── Package API ───────────────────────────────────────────────


class TestPackageAPI(unittest.TestCase):

    def test_run_pipeline(self):
        from engine import run_pipeline
        self.assertTrue(callable(run_pipeline))

    def test_pipeline_engine(self):
        from engine import PipelineEngine
        self.assertTrue(callable(PipelineEngine))

    def test_errors(self):
        from engine import WorkerError, WorkerSpawnError
        self.assertTrue(issubclass(WorkerSpawnError, WorkerError))

    def test_version(self):
        import engine
        self.assertEqual(engine.__version__, "2.0.0")


if __name__ == "__main__":
    unittest.main()
