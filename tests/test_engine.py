"""
Test suite for the engine.

Covers: exception hierarchy, AST-based METADATA extraction, phase splitting,
per-environment workers with state dicts, worker pool with per-step
concurrency (semaphores), Engine API (register/submit/status, the job
board), concurrent execution,
graceful failure handling, and lifecycle management.

Structure
---------
- TestErrors                  Exception hierarchy
- TestLoader                  AST-based METADATA extraction
- TestPhases                  Phase splitting from YAML step lists
- TestWorkerProtocol          Per-env workers, state dict, caching
- TestWorkerErrorPaths        Crash, timeout, missing file
- TestPool                    Per-env pools, semaphores
- TestEngineRegister          Pipeline registration
- TestEngineSubmit            Job submission, immediate execution
- TestEngineConcurrency       Parallel jobs, max_workers
- TestEngineErrors            Graceful failure handling
- TestEngineLifecycle         Shutdown, context manager
- TestEngineStatus            The job board
- TestEngineMultiPipeline     Multiple registered pipelines
- TestEnginePriority          Priority orders queued jobs
- TestPackageAPI              Public exports and versioning
- TestTheBrake                A shutdown that does not wait

Usage
-----
    python -m pytest tests/test_engine.py -v
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# Step files, pipelines and the polling helpers are the shared ones from helpers.py.
from helpers import (
    _SESSION_TEMP,
    _next_id,
    _wait_for_results,
    _wait_for_status,
    failures,
    fresh_output,
    phase_of,
    tally,
)
from helpers import (
    _write_step as _temp_step,
)
from helpers import (
    _write_yaml as _temp_yaml,
)

from zmart_analysis import (
    ScopeError,
    StepExecutionError,
    WorkerCrashedError,
    WorkerError,
    WorkerSpawnError,
    WorkerTimeoutError,
)
from zmart_analysis.pipeline import get_step_settings, split_phases

_TEMP_DIR = str(_SESSION_TEMP)


def _capture_exception(errors, function):
    """Run ``function`` and append any raised exception for thread assertions."""
    try:
        function()
    except BaseException as exc:
        errors.append(exc)


# ---- Errors ----------------------------------------------------------


class TestErrors:
    def test_worker_hierarchy(self):
        assert issubclass(WorkerSpawnError, WorkerError)
        assert issubclass(WorkerCrashedError, WorkerError)
        assert issubclass(WorkerTimeoutError, WorkerError)
        assert issubclass(StepExecutionError, WorkerError)

    def test_timeout_error_is_not_step_execution_error(self):
        # A killed-on-timeout worker is an infrastructure failure, not a
        # user-code exception, so the two types must stay distinct.
        assert not issubclass(WorkerTimeoutError, StepExecutionError)
        assert not issubclass(StepExecutionError, WorkerTimeoutError)

    def test_step_execution_error_stores_traceback(self):
        err = StepExecutionError("boom", remote_traceback="tb")
        assert str(err) == "boom"
        assert err.remote_traceback == "tb"

    def test_step_execution_error_traceback_default_none(self):
        assert StepExecutionError("x").remote_traceback is None

    def test_scope_error_independent(self):
        assert not issubclass(ScopeError, WorkerError)
        assert issubclass(ScopeError, Exception)


# ---- Loader ----------------------------------------------------------


class TestLoader:
    def test_defaults_no_metadata(self, tmp_path):
        path = tmp_path / "bare.py"
        path.write_text("def run(pd, state, **p): return pd")
        s = get_step_settings(path)
        assert s["environment"] is None
        assert s["max_workers"] == 1

    def test_explicit_environment(self):
        path = _temp_step("""
            METADATA = {"environment": "gpu_env"}
            def run(pd, state, **p): return pd
        """)
        s = get_step_settings(Path(path))
        assert s["environment"] == "gpu_env"

    def test_max_workers(self):
        path = _temp_step("""
            METADATA = {"max_workers": 5}
            def run(pd, state, **p): return pd
        """)
        s = get_step_settings(Path(path))
        assert s["max_workers"] == 5

    def test_max_workers_default_1(self):
        path = _temp_step('METADATA = {"environment": "some_env"}')
        s = get_step_settings(Path(path))
        assert s["max_workers"] == 1

    def test_does_not_execute_module_code(self):
        path = _temp_step("""
            import nonexistent_package_xyz
            METADATA = {"environment": "safe", "max_workers": 3}
            def run(pd, state, **p): return pd
        """)
        s = get_step_settings(Path(path))
        assert s["environment"] == "safe"
        assert s["max_workers"] == 3


# ---- Phases ----------------------------------------------------------


class TestPhases:
    def test_no_scope_single_phase(self):
        steps = [{"a": None}, {"b": {"x": 1}}]
        phases = split_phases(steps)
        assert len(phases) == 1
        assert phases[0].scope is None
        assert phases[0].steps[0].name == "a"
        assert phases[0].steps[1].params == {"x": 1}

    def test_a_pipeline_may_set_a_steps_concurrency(self):
        """``max_workers`` on a step in the YAML is the engine's key, not a param."""
        steps = [{"a": {"max_workers": 8, "x": 1}}, {"b": None}]
        phases = split_phases(steps)
        assert phases[0].steps[0].max_workers == 8
        assert phases[0].steps[0].params == {"x": 1}
        assert phases[0].steps[1].max_workers is None

    def test_one_scope_two_phases(self):
        steps = [
            {"preprocess": None},
            {"segment": None},
            {"stitch": {"scope": "group"}},
            {"analyze": None},
        ]
        phases = split_phases(steps)
        assert len(phases) == 2
        assert phases[0].scope is None
        assert [s.name for s in phases[0].steps] == ["preprocess", "segment"]
        assert phases[1].scope == "group"
        assert [s.name for s in phases[1].steps] == ["stitch", "analyze"]

    def test_two_scopes_three_phases(self):
        steps = [
            {"a": None},
            {"b": {"scope": "group"}},
            {"c": None},
            {"d": {"scope": "all"}},
        ]
        phases = split_phases(steps)
        assert len(phases) == 3
        assert phases[0].scope is None
        assert phases[1].scope == "group"
        assert phases[2].scope == "all"

    def test_scope_params_separated(self):
        steps = [{"step": {"scope": "region", "sigma": 1.0}}]
        phases = split_phases(steps)
        assert phases[0].steps[0].params == {"sigma": 1.0}
        assert "scope" not in phases[0].steps[0].params

    def test_scope_on_first_step(self):
        steps = [{"a": {"scope": "region"}}, {"b": None}]
        phases = split_phases(steps)
        assert len(phases) == 1
        assert phases[0].scope == "region"

    def test_a_pipeline_may_not_set_a_steps_environment(self):
        """The step file owns its environment; a pipeline that tries to set
        one is refused with a message saying where it belongs."""
        with pytest.raises(ValueError, match="step file owns its environment"):
            split_phases([{"step": {"environment": "other-env", "sigma": 1.0}}])

    def test_a_steps_params_do_not_include_the_engine_keys(self):
        phases = split_phases([{"step": {"max_workers": 3, "sigma": 1.0}}])
        assert phases[0].steps[0].params == {"sigma": 1.0}
        assert phases[0].steps[0].max_workers == 3


# ---- Worker (protocol) -----------------------------------------------


class TestWorkerProtocol:
    def test_execute_returns_result(self):
        from zmart_analysis.workers import Worker

        path = _temp_step("""
            def run(pd, state, **p):
                pd["ran"] = True
                pd["x"] = p.get("x")
                return pd
        """)
        w = Worker(environment=None, connect_timeout=10)
        try:
            result = w.execute([(path, {"x": 42})], {"input": 1}, timeout=10)
        finally:
            w.shutdown()
        assert result["ran"]
        assert result["x"] == 42
        assert result["input"] == 1

    def test_different_steps_same_worker(self):
        from zmart_analysis.workers import Worker

        path_a = _temp_step("""
            def run(pd, state, **p): pd["from"] = "a"; return pd
        """)
        path_b = _temp_step("""
            def run(pd, state, **p): pd["from"] = "b"; return pd
        """)
        w = Worker(environment=None, connect_timeout=10)
        try:
            ra = w.execute([(path_a, {})], {}, timeout=10)
            rb = w.execute([(path_b, {})], {}, timeout=10)
            assert ra["from"] == "a"
            assert rb["from"] == "b"
        finally:
            w.shutdown()

    def test_module_caching(self):
        from zmart_analysis.workers import Worker

        path = _temp_step("""
            _n = 0
            def run(pd, state, **p):
                global _n
                _n += 1
                pd["n"] = _n
                return pd
        """)
        w = Worker(environment=None, connect_timeout=10)
        try:
            r1 = w.execute([(path, {})], {}, timeout=10)
            r2 = w.execute([(path, {})], {}, timeout=10)
            assert r1["n"] == 1
            assert r2["n"] == 2
        finally:
            w.shutdown()

    def test_state_dict_persists(self):
        """State dict persists across calls for the same step."""
        from zmart_analysis.workers import Worker

        path = _temp_step("""
            def run(pd, state, **p):
                state.setdefault("count", 0)
                state["count"] += 1
                pd["count"] = state["count"]
                return pd
        """)
        w = Worker(environment=None, connect_timeout=10)
        try:
            r1 = w.execute([(path, {})], {}, timeout=10)
            r2 = w.execute([(path, {})], {}, timeout=10)
            r3 = w.execute([(path, {})], {}, timeout=10)
            assert r1["count"] == 1
            assert r2["count"] == 2
            assert r3["count"] == 3
        finally:
            w.shutdown()

    def test_a_step_is_a_real_module_in_the_worker(self):
        """A step is registered under its name, like an imported module.

        Two things that look a module up by name are tried: a dataclass with
        postponed annotations (``from __future__ import annotations``), which
        resolves them through ``sys.modules``, and a second step importing
        the first as a sibling. Both failed when the worker ran step files
        with ``exec`` into a bare namespace.
        """
        from zmart_analysis.workers import Worker

        _temp_step(
            """
            from __future__ import annotations
            from dataclasses import dataclass

            @dataclass
            class Measured:
                value: int | None = None

            def run(pd, state, **p):
                pd["measured"] = Measured(pd["input"]["n"]).value
                return pd
        """,
            name="loader_with_dataclass",
        )
        sibling = _temp_step(
            """
            import sys
            from pathlib import Path
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from loader_with_dataclass import run as _inner

            def run(pd, state, **p):
                pd = _inner(pd, state, **p)
                pd["via"] = __name__
                return pd
        """,
            name="loader_sibling",
        )
        w = Worker(environment=None)
        try:
            out = w.execute([(sibling, {})], {"input": {"n": 7}})
            assert out["measured"] == 7
            assert out["via"] == "loader_sibling"
        finally:
            w.shutdown()

    def test_state_dict_isolated_per_step(self):
        """Different steps get separate state dicts."""
        from zmart_analysis.workers import Worker

        path_a = _temp_step("""
            def run(pd, state, **p):
                state.setdefault("key", "a")
                pd["state_key"] = state["key"]
                return pd
        """)
        path_b = _temp_step("""
            def run(pd, state, **p):
                state.setdefault("key", "b")
                pd["state_key"] = state["key"]
                return pd
        """)
        w = Worker(environment=None, connect_timeout=10)
        try:
            ra = w.execute([(path_a, {})], {}, timeout=10)
            rb = w.execute([(path_b, {})], {}, timeout=10)
            assert ra["state_key"] == "a"
            assert rb["state_key"] == "b"
        finally:
            w.shutdown()

    def test_persistent_reuses_process(self):
        from zmart_analysis.workers import Worker

        path = _temp_step("""
            import os
            def run(pd, state, **p): pd["pid"] = os.getpid(); return pd
        """)
        w = Worker(environment=None, connect_timeout=10)
        try:
            r1 = w.execute([(path, {})], {}, timeout=10)
            r2 = w.execute([(path, {})], {}, timeout=10)
            assert r1["pid"] == r2["pid"]
        finally:
            w.shutdown()

    def test_shutdown_and_respawn(self):
        from zmart_analysis.workers import Worker

        path = _temp_step("""
            import os
            def run(pd, state, **p): pd["pid"] = os.getpid(); return pd
        """)
        w = Worker(environment=None, connect_timeout=10)
        try:
            r1 = w.execute([(path, {})], {}, timeout=10)
            w.shutdown()
            r2 = w.execute([(path, {})], {}, timeout=10)
            assert r1["pid"] != r2["pid"]
        finally:
            w.shutdown()

    def test_complex_types(self):
        from zmart_analysis.workers import Worker

        path = _temp_step("def run(pd, state, **p): return pd")
        data = {
            "tuple": (1, 2),
            "set": {3, 4},
            "bytes": b"\xff",
            "nested": {"a": [None, True, {"b": 2.5}]},
        }
        w = Worker(environment=None, connect_timeout=10)
        try:
            r = w.execute([(path, {})], data, timeout=10)
        finally:
            w.shutdown()
        assert r["tuple"] == (1, 2)
        assert r["set"] == {3, 4}
        assert r["nested"]["a"][2]["b"] == 2.5

    def test_worker_status(self):
        from zmart_analysis.workers import Worker

        w = Worker(environment=None, connect_timeout=10)
        s = w.status
        assert s["state"] == "stopped"
        w.shutdown()


# ---- Worker Error Paths ----------------------------------------------


class TestWorkerErrorPaths:
    def test_crash_raises_worker_crashed(self):
        from zmart_analysis.workers import Worker

        path = _temp_step("""
            import os
            def run(pd, state, **p): os._exit(1)
        """)
        w = Worker(environment=None, connect_timeout=10)
        with pytest.raises(WorkerCrashedError):
            w.execute([(path, {})], {}, timeout=10)
        w.shutdown()

    def test_timeout_raises_worker_timeout_error(self):
        from zmart_analysis.workers import Worker

        path = _temp_step("""
            import time
            def run(pd, state, **p): time.sleep(30); return pd
        """)
        w = Worker(environment=None, connect_timeout=10)
        with pytest.raises(WorkerTimeoutError) as ctx:
            w.execute([(path, {})], {}, timeout=1)
        assert "timed out" in str(ctx.value)
        # A slow step is not a user-code exception; keep the types distinct.
        assert not isinstance(ctx.value, StepExecutionError)
        w.shutdown()

    def test_step_error_has_traceback(self):
        from zmart_analysis.workers import Worker

        path = _temp_step("""
            def run(pd, state, **p): raise ValueError("test")
        """)
        w = Worker(environment=None, connect_timeout=10)
        with pytest.raises(StepExecutionError) as ctx:
            w.execute([(path, {})], {}, timeout=10)
        assert "test" in str(ctx.value)
        assert "ValueError" in ctx.value.remote_traceback
        w.shutdown()

    def test_spawn_command_passes_engine_pid_as_parent_pid(self):
        # Orphan detection must watch the engine PID explicitly, because a
        # conda-env worker's real parent is the `conda run` wrapper.
        import subprocess as _sp

        from zmart_analysis import workers

        captured = {}
        real_popen = _sp.Popen

        def fake_popen(cmd, *a, **k):
            captured["cmd"] = list(cmd)
            # Fail the spawn immediately so no real subprocess is created;
            # we only care about the command that would have been run.
            raise OSError("blocked for test")

        w = workers.Worker(environment=None, connect_timeout=1)
        workers.subprocess.Popen = fake_popen
        try:
            with pytest.raises(WorkerSpawnError):
                w.ensure_running()
        finally:
            workers.subprocess.Popen = real_popen
            w.shutdown()

        cmd = captured["cmd"]
        assert "--parent-pid" in cmd
        pid_arg = cmd[cmd.index("--parent-pid") + 1]
        assert pid_arg == str(os.getpid())

    def test_worker_exits_when_watched_parent_pid_dies(self):
        # End-to-end: the worker watches the --parent-pid it is given, not
        # its real parent. Here the worker's real parent is this test process
        # (alive throughout); its watched parent is a throwaway process we
        # kill. The worker must exit anyway -- proving it does not rely on
        # os.getppid(), which is the conda-wrapper failure mode.
        import subprocess as _sp
        from multiprocessing.connection import Listener

        from zmart_analysis.workers import WORKER_SCRIPT

        authkey = os.urandom(16)
        listener = Listener(("localhost", 0), authkey=authkey)
        port = listener.address[1]
        listener._listener._socket.settimeout(15)

        fake_parent = _sp.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        worker = _sp.Popen(
            [
                sys.executable,
                str(WORKER_SCRIPT),
                "--port",
                str(port),
                "--authkey",
                authkey.hex(),
                "--parent-pid",
                str(fake_parent.pid),
            ],
            stdout=_sp.DEVNULL,
            stderr=_sp.DEVNULL,
        )

        try:
            conn = listener.accept()  # worker connected -> it is running
            assert worker.poll() is None, "worker exited before parent died"

            fake_parent.terminate()
            fake_parent.wait(timeout=10)

            # Worker polls parent liveness on a <=5s cycle; allow margin.
            worker.wait(timeout=20)
            assert worker.poll() is not None, "worker did not exit after its watched parent died"
            conn.close()
        finally:
            for proc in (worker, fake_parent):
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)
            listener.close()

    def test_windows_parent_check_distinguishes_running_and_terminated(self, subtests):
        from zmart_analysis.worker_script import windows_process_alive

        class FakeKernel32:
            def __init__(self, wait_result, handle=123, error=0):
                self.wait_result = wait_result
                self.handle = handle
                self.error = error
                self.closed = []

            def GetLastError(self):
                return self.error

            def OpenProcess(self, access, inherit, process_id):
                self.open_args = (access, inherit, process_id)
                return self.handle

            def WaitForSingleObject(self, handle, timeout_ms):
                self.wait_args = (handle, timeout_ms)
                return self.wait_result

            def CloseHandle(self, handle):
                self.closed.append(handle)

        cases = (
            (0x00000102, True),  # WAIT_TIMEOUT: process still running
            (0x00000000, False),  # WAIT_OBJECT_0: process terminated
            (0xFFFFFFFF, False),  # WAIT_FAILED: do not claim it is alive
        )
        for wait_result, expected in cases:
            with subtests.test(wait_result=wait_result):
                kernel32 = FakeKernel32(wait_result)
                assert windows_process_alive(4321, kernel32) == expected
                assert kernel32.open_args == (0x00100000, False, 4321)
                assert kernel32.wait_args == (123, 0)
                assert kernel32.closed == [123]

        unavailable = FakeKernel32(0x00000102, handle=0)
        assert not windows_process_alive(4321, unavailable)
        assert unavailable.closed == []

        # Another user's process may not be opened: it is there, so alive.
        refused = FakeKernel32(0x00000102, handle=0, error=5)  # ERROR_ACCESS_DENIED
        assert windows_process_alive(4321, refused)


# ---- Pool ------------------------------------------------------------


class TestPool:
    def test_a_steps_concurrency_is_its_own_at_each_width(self):
        """Two pipelines sharing a step file may run it at different widths.

        The step's gate used to be created once per step file with whichever
        width was seen first, so a pipeline that asked for eight got one, or
        the other way round, depending on registration order.
        """
        from zmart_analysis.worker_pool import WorkerPool

        pool = WorkerPool()
        try:
            one = pool.gate_for("/steps/detect.py", 1)
            eight = pool.gate_for("/steps/detect.py", 8)
            assert one is not eight
            assert pool.gate_for("/steps/detect.py", 8) is eight
            assert eight.width == 8
        finally:
            pool.shutdown_all(now=True)

    def test_per_env_worker_reuse(self):
        from zmart_analysis.worker_pool import WorkerPool

        path = _temp_step("""
            import os
            def run(pd, state, **p): pd["pid"] = os.getpid(); return pd
        """)
        pool = WorkerPool()
        r1 = pool.execute(None, [(path, {}, 1)], {}, timeout=10)
        r2 = pool.execute(None, [(path, {}, 1)], {}, timeout=10)
        assert r1["pid"] == r2["pid"]
        pool.shutdown_all()

    def test_shutdown_before_use(self):
        from zmart_analysis.worker_pool import WorkerPool

        path = _temp_step("def run(pd, state, **p): return pd")
        pool = WorkerPool()
        pool.shutdown_all()
        with pytest.raises(RuntimeError, match="shut down"):
            pool.execute(None, [(path, {}, 1)], {}, timeout=10)

    def test_error_through_pool(self):
        from zmart_analysis.worker_pool import WorkerPool

        path = _temp_step("""
            def run(pd, state, **p): raise ValueError("pool err")
        """)
        pool = WorkerPool()
        with pytest.raises(StepExecutionError) as ctx:
            pool.execute(None, [(path, {}, 1)], {}, timeout=10)
        assert "pool err" in str(ctx.value)
        pool.shutdown_all()

    def test_semaphore_limits_concurrency(self):
        """max_workers=1 serializes execution of the same step."""
        from zmart_analysis.worker_pool import WorkerPool

        path = _temp_step("""
            import time
            def run(pd, state, **p):
                time.sleep(0.3)
                pd["done"] = True
                return pd
        """)
        pool = WorkerPool()
        results = []

        def run_one():
            r = pool.execute(None, [(path, {}, 1)], {}, timeout=15)
            results.append(r)

        t0 = time.monotonic()
        threads = [threading.Thread(target=run_one) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        elapsed = time.monotonic() - t0
        pool.shutdown_all()

        assert len(results) == 2
        assert elapsed > 0.5, "max_workers=1 should serialize execution"

    def test_semaphore_allows_parallelism(self):
        """max_workers=4 allows parallel execution."""
        from zmart_analysis.worker_pool import WorkerPool

        path = _temp_step("""
            import time
            def run(pd, state, **p):
                pd["start"] = time.time()
                time.sleep(1.5)
                pd["end"] = time.time()
                return pd
        """)
        pool = WorkerPool()
        results = []

        def run_one():
            r = pool.execute(None, [(path, {}, 4)], {}, timeout=15)
            results.append(r)

        threads = [threading.Thread(target=run_one) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        pool.shutdown_all()

        assert len(results) == 4
        # All four were running at one moment: the last to start began
        # before the first to finish ended. Measured from inside the steps,
        # so the time it takes to start a worker process does not count;
        # a wall-clock limit failed on slow two-core machines.
        last_start = max(r["start"] for r in results)
        first_end = min(r["end"] for r in results)
        assert last_start < first_end, "max_workers=4 should allow parallel execution"


# ---- Engine (register) -----------------------------------------------


class TestEngineRegister:
    def test_register_simple(self):
        _temp_step("def run(pd, state, **p): return pd", name="reg_a")
        yaml = _temp_yaml("wf:\n  - reg_a:")
        from zmart_analysis import Engine

        with Engine() as e:
            e.register("test", yaml, fresh_output())

    def test_register_duplicate_raises(self):
        _temp_step("def run(pd, state, **p): return pd", name="reg_b")
        yaml = _temp_yaml("wf:\n  - reg_b:")
        from zmart_analysis import Engine

        with Engine() as e:
            e.register("test", yaml, fresh_output())
            with pytest.raises(ValueError):
                e.register("test", yaml, fresh_output())

    def test_register_bad_yaml(self):
        path = Path(_TEMP_DIR) / f"bad_{_next_id()}.yaml"
        path.write_text("metadata:\n  functions_dir: .")
        from zmart_analysis import Engine

        with Engine() as e:
            with pytest.raises(ValueError):
                e.register("bad", str(path), fresh_output())

    def test_concurrent_register_same_name_raises_once(self):
        # The duplicate check and the insert must be atomic: with the check
        # and insert split across two lock holds, two threads racing on the
        # same name both passed and the second silently clobbered the first.
        _temp_step("def run(pd, state, **p): return pd", name="reg_race")
        yaml = _temp_yaml("wf:\n  - reg_race:")
        from zmart_analysis import Engine

        with Engine() as e:
            start = threading.Barrier(8)
            errors = []
            lock = threading.Lock()

            def worker():
                start.wait()
                try:
                    e.register("dup", yaml, fresh_output())
                except ValueError:
                    with lock:
                        errors.append(1)

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # Exactly one registration wins; every other thread sees ValueError.
            assert sum(errors) == 7
            assert "dup" in e.pipelines

    def test_every_result_records_where_each_step_ran(self):
        """Provenance: each step's result names its environment, the Python
        it ran on, a fingerprint of every installed package, and the
        version of each package it actually imported."""
        _temp_step(
            """
            import yaml
            def run(pd, state, **p):
                pd["seen"] = True
                return pd
        """,
            name="reg_provenance",
        )
        from zmart_analysis import Engine

        with Engine() as e:
            e.register("test", _temp_yaml("wf:\n  - reg_provenance:"), fresh_output())
            e.submit("test", {})
            results = _wait_for_results(e, "test", 1, timeout=30)
        record = results[0]["provenance"]["reg_provenance"]
        assert set(record) == {"environment", "python", "fingerprint", "packages"}
        assert len(record["fingerprint"]) == 16
        assert "pyyaml" in record["packages"]


# ---- Engine (submit) -------------------------------------------------


class TestEngineSubmit:
    def test_simple_submit(self):
        _temp_step(
            """
            def run(pd, state, **p):
                pd["ok"] = True
                return pd
        """,
            name="sub_a",
        )
        yaml = _temp_yaml("wf:\n  - sub_a:")
        from zmart_analysis import Engine

        with Engine() as e:
            e.register("test", yaml, fresh_output())
            e.submit("test", {})
            results = _wait_for_results(e, "test", 1, timeout=10)
        assert len(results) > 0
        assert results[0]["ok"]

    def test_multi_step(self):
        _temp_step(
            """
            def run(pd, state, **p):
                pd["s1"] = 1
                return pd
        """,
            name="ms_a",
        )
        _temp_step(
            """
            def run(pd, state, **p):
                pd["s2"] = pd["s1"] + 1
                return pd
        """,
            name="ms_b",
        )
        yaml = _temp_yaml("wf:\n  - ms_a:\n  - ms_b:")
        from zmart_analysis import Engine

        with Engine() as e:
            e.register("test", yaml, fresh_output())
            e.submit("test", {})
            results = _wait_for_results(e, "test", 1, timeout=10)
        assert results[0]["s1"] == 1
        assert results[0]["s2"] == 2

    def test_data_flows_between_steps(self):
        _temp_step(
            """
            def run(pd, state, **p):
                pd["from_a"] = "hello"
                return pd
        """,
            name="df_a",
        )
        _temp_step(
            """
            def run(pd, state, **p):
                pd["saw"] = pd.get("from_a")
                return pd
        """,
            name="df_b",
        )
        yaml = _temp_yaml("wf:\n  - df_a:\n  - df_b:")
        from zmart_analysis import Engine

        with Engine() as e:
            e.register("test", yaml, fresh_output())
            e.submit("test", {})
            results = _wait_for_results(e, "test", 1, timeout=10)
        assert results[0]["saw"] == "hello"

    def test_input_data(self):
        _temp_step("def run(pd, state, **p): return pd", name="inp")
        yaml = _temp_yaml("wf:\n  - inp:")
        from zmart_analysis import Engine

        with Engine() as e:
            e.register("test", yaml, fresh_output())
            e.submit("test", {"key": "val"})
            results = _wait_for_results(e, "test", 1, timeout=10)
        assert results[0]["input"]["key"] == "val"

    def test_params_from_yaml(self):
        _temp_step(
            """
            def run(pd, state, **p):
                pd["x"] = p.get("x")
                return pd
        """,
            name="par",
        )
        yaml = _temp_yaml("wf:\n  - par:\n      x: 42")
        from zmart_analysis import Engine

        with Engine() as e:
            e.register("test", yaml, fresh_output())
            e.submit("test", {})
            results = _wait_for_results(e, "test", 1, timeout=10)
        assert results[0]["x"] == 42

    def test_concurrent_submits(self):
        _temp_step(
            """
            def run(pd, state, **p):
                pd["job"] = pd["input"]["job"]
                return pd
        """,
            name="conc",
        )
        yaml = _temp_yaml("wf:\n  - conc:")
        from zmart_analysis import Engine

        with Engine() as e:
            e.register("test", yaml, fresh_output())
            for i in range(5):
                e.submit("test", {"job": i})
            results = _wait_for_results(e, "test", 5, timeout=15)
        assert len(results) == 5
        assert sorted(r["job"] for r in results) == list(range(5))


# ---- Engine (concurrency) -------------------------------------------


class TestEngineConcurrency:
    def test_a_yaml_max_workers_below_one_is_refused_at_register(self):
        _temp_step("def run(pd, state, **p): return pd", name="narrow")
        yaml = _temp_yaml("wf:\n  - narrow:\n      max_workers: 0")
        from zmart_analysis import Engine

        with Engine() as e:
            with pytest.raises(ValueError):
                e.register("test", yaml, fresh_output())

    def test_a_yaml_max_workers_reaches_the_steps_chain(self):
        _temp_step("def run(pd, state, **p): return pd", name="wide")
        yaml = _temp_yaml("wf:\n  - wide:\n      max_workers: 3")
        from zmart_analysis import Engine

        with Engine() as e:
            e.register("test", yaml, fresh_output())
            assert e.pipelines["test"].chains[0][0].widths == [3]

    def test_many_concurrent_jobs(self):
        _temp_step(
            """
            def run(pd, state, **p):
                pd["idx"] = pd["input"]["idx"]
                return pd
        """,
            name="many",
        )
        yaml = _temp_yaml("wf:\n  - many:")
        from zmart_analysis import Engine

        with Engine(max_concurrent=8) as e:
            e.register("test", yaml, fresh_output())
            for i in range(20):
                e.submit("test", {"idx": i})
            results = _wait_for_results(e, "test", 20, timeout=30)
        assert len(results) == 20
        assert sorted(r["idx"] for r in results) == list(range(20))


# ---- Engine (errors) -------------------------------------------------


class TestEngineErrors:
    def test_failed_job_does_not_crash_pipeline(self):
        """Other jobs continue when one fails."""
        _temp_step(
            """
            def run(pd, state, **p):
                if pd["input"].get("fail"):
                    raise ValueError("deliberate failure")
                pd["ok"] = True
                return pd
        """,
            name="graceful",
        )
        yaml = _temp_yaml("wf:\n  - graceful:")
        from zmart_analysis import Engine

        with Engine() as e:
            e.register("test", yaml, fresh_output())
            e.submit("test", {"fail": True})
            e.submit("test", {"fail": False})
            e.submit("test", {"fail": False})
            board = _wait_for_status(e, "test", 3, timeout=15)
        assert tally(board)["done"] == 2
        assert tally(board)["failed"] == 1
        assert board[1]["state"] == "failed"
        assert board[2]["output"]["ok"] and board[3]["output"]["ok"]

    def test_failures_in_status(self):
        _temp_step(
            """
            def run(pd, state, **p): raise RuntimeError("boom")
        """,
            name="fail_status",
        )
        yaml = _temp_yaml("wf:\n  - fail_status:")
        from zmart_analysis import Engine

        with Engine() as e:
            e.register("test", yaml, fresh_output())
            e.submit("test", {})
            board = _wait_for_status(e, "test", 1, timeout=10)
        assert tally(board)["failed"] == 1
        (failed,) = failures(board)
        assert failed["job"] == 1
        assert failed["step"] == "fail_status"
        assert "boom" in failed["error"]

    def test_return_non_dict_raises(self):
        _temp_step(
            """
            def run(pd, state, **p): return "not a dict"
        """,
            name="bad_ret",
        )
        yaml = _temp_yaml("wf:\n  - bad_ret:")
        from zmart_analysis import Engine

        with Engine() as e:
            e.register("test", yaml, fresh_output())
            e.submit("test", {})
            board = _wait_for_status(e, "test", 1, timeout=10)
        assert tally(board)["failed"] == 1
        assert "returned str, expected dict" in board[1]["error"]


# ---- Engine (lifecycle) ----------------------------------------------


class TestEngineLifecycle:
    def test_context_manager(self):
        _temp_step("def run(pd, state, **p): return pd", name="ctx")
        yaml = _temp_yaml("wf:\n  - ctx:")
        from zmart_analysis import Engine

        with Engine() as e:
            e.register("test", yaml, fresh_output())
            e.submit("test", {})
            results = _wait_for_results(e, "test", 1, timeout=10)
        assert len(results) == 1

    def test_shutdown_then_register_raises(self):
        """register() after shutdown raises RuntimeError."""
        from zmart_analysis import Engine

        _temp_step("def run(pd, state, **p): return pd", name="shut_reg")
        yaml = _temp_yaml("wf:\n  - shut_reg:")
        e = Engine()
        e.shutdown()
        with pytest.raises(RuntimeError):
            e.register("test", yaml, fresh_output())

    def test_concurrent_registration_reserves_pipeline_name(self):
        """Only one parser may build a given pipeline name at a time."""
        import zmart_analysis.engine as engine_module
        from zmart_analysis import Engine

        _temp_step("def run(pd, state, **p): return pd", name="reg_race")
        yaml = _temp_yaml("wf:\n  - reg_race:")
        parse_started = threading.Event()
        release_parse = threading.Event()
        real_parse_yaml = engine_module.parse_yaml
        errors = []

        def blocked_parse_yaml(path):
            parse_started.set()
            if not release_parse.wait(timeout=5):
                raise TimeoutError("test did not release YAML parsing")
            return real_parse_yaml(path)

        e = Engine()
        with patch("zmart_analysis.engine.parse_yaml", blocked_parse_yaml):
            thread = threading.Thread(
                target=lambda: _capture_exception(
                    errors, lambda: e.register("test", yaml, fresh_output())
                )
            )
            thread.start()
            assert parse_started.wait(timeout=5)
            with pytest.raises(ValueError, match="already registered"):
                e.register("test", yaml, fresh_output())
            release_parse.set()
            thread.join(timeout=5)

        assert not thread.is_alive()
        assert errors == []
        assert "test" in e.status()
        e.shutdown()

    def test_registration_cannot_complete_after_shutdown_starts(self):
        """A registration parsing during shutdown must not become visible."""
        import zmart_analysis.engine as engine_module
        from zmart_analysis import Engine

        _temp_step("def run(pd, state, **p): return pd", name="reg_shutdown")
        yaml = _temp_yaml("wf:\n  - reg_shutdown:")
        parse_started = threading.Event()
        release_parse = threading.Event()
        real_parse_yaml = engine_module.parse_yaml
        errors = []

        def blocked_parse_yaml(path):
            parse_started.set()
            if not release_parse.wait(timeout=5):
                raise TimeoutError("test did not release YAML parsing")
            return real_parse_yaml(path)

        e = Engine()
        with patch("zmart_analysis.engine.parse_yaml", blocked_parse_yaml):
            thread = threading.Thread(
                target=lambda: _capture_exception(
                    errors, lambda: e.register("late", yaml, fresh_output())
                )
            )
            thread.start()
            assert parse_started.wait(timeout=5)
            e.shutdown(wait=False)
            release_parse.set()
            thread.join(timeout=5)

        assert not thread.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], RuntimeError)
        assert "late" not in e.status()

    def test_failed_registration_releases_pipeline_name(self):
        """A parse failure must not leave the name permanently reserved."""
        from zmart_analysis import Engine

        _temp_step("def run(pd, state, **p): return pd", name="reg_retry")
        invalid_yaml = _temp_yaml("wf: [")
        valid_yaml = _temp_yaml("wf:\n  - reg_retry:")
        e = Engine()
        with pytest.raises(Exception):
            e.register("retry", invalid_yaml, fresh_output())
        e.register("retry", valid_yaml, fresh_output())
        assert "retry" in e.status()
        e.shutdown()

    def test_shutdown_then_submit_raises(self):
        """submit() after shutdown raises RuntimeError."""
        _temp_step("def run(pd, state, **p): return pd", name="shut_sub")
        yaml = _temp_yaml("wf:\n  - shut_sub:")
        from zmart_analysis import Engine

        e = Engine()
        e.register("test", yaml, fresh_output())
        e.shutdown()
        with pytest.raises(RuntimeError):
            e.submit("test", {})

    def test_double_shutdown(self):
        from zmart_analysis import Engine

        e = Engine()
        e.shutdown()
        e.shutdown()  # should not raise

    def test_shutdown_without_wait_cancels_queue_and_closes_workers(self):
        """Twelve 0.3 s steps on one thread would take 3.6 s to run out; a
        shutdown that cancels the queue returns long before. The board goes
        with the pipelines, so what is left to see is the time it took, that
        no pipeline remains and that no worker is left."""
        _temp_step(
            """
            import time
            def run(pd, state, **p):
                time.sleep(0.3)
                return pd
        """,
            name="shutdown_slow",
        )
        yaml = _temp_yaml("wf:\n  - shutdown_slow:")
        from zmart_analysis import Engine

        e = Engine(max_concurrent=1)
        e.register("test", yaml, fresh_output())
        for i in range(12):
            e.submit("test", {"i": i})

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if tally(e.status("test"))["running"] == 1:
                break
            time.sleep(0.01)
        counts = tally(e.status("test"))
        assert counts["running"] == 1
        assert counts["queued"] + counts["done"] == 11

        t0 = time.monotonic()
        e.shutdown(wait=False)
        assert time.monotonic() - t0 < 2.0

        assert e.status() == {}
        assert e.workers.status["workers"] == []


# ---- Engine (status) -------------------------------------------------


class TestEngineStatus:
    def test_status_single_pipeline(self):
        _temp_step("def run(pd, state, **p): return pd", name="st")
        yaml = _temp_yaml("wf:\n  - st:")
        from zmart_analysis import Engine

        with Engine() as e:
            e.register("test", yaml, fresh_output())
            e.submit("test", {})
            board = _wait_for_status(e, "test", 1, timeout=10)
        assert list(board) == [1]
        assert board[1]["state"] == "done"
        assert board[1]["step"] == "st"
        assert board[1]["scope"] == {}
        assert board[1]["output"]["input"] == {}

    def test_status_all_pipelines(self):
        _temp_step("def run(pd, state, **p): return pd", name="st2")
        yaml = _temp_yaml("wf:\n  - st2:")
        from zmart_analysis import Engine

        with Engine() as e:
            e.register("a", yaml, fresh_output())
            e.register("b", yaml, fresh_output())
            boards = e.status()
        assert boards == {"a": {}, "b": {}}

    def test_status_nonexistent_raises(self):
        from zmart_analysis import Engine

        with Engine() as e:
            with pytest.raises(KeyError):
                e.status("ghost")

    def test_a_failed_tile_stays_on_the_board_after_its_group_closes(self):
        """Scope collection hands the failed tile to the group step; the
        board keeps it failed all the same, with its step and error, so a
        caller polling the board never loses a failure to a scope close."""
        _temp_step(
            """
            def run(pd, state, **p):
                if pd["input"]["tile"] == 1:
                    raise ValueError("bad tile")
                pd["tile"] = pd["input"]["tile"]
                return pd
        """,
            name="drift_seg",
        )
        _temp_step(
            """
            def run(pd, state, **p):
                pd["tiles"] = sorted(part["output"]["tile"] for part in pd["parts"])
                return pd
        """,
            name="drift_stitch",
        )
        yaml = _temp_yaml("""
            wf:
              - drift_seg:
              - drift_stitch:
                  scope: group
        """)
        from zmart_analysis import Engine

        with Engine() as e:
            e.register("test", yaml, fresh_output())
            for i in range(3):
                complete = "group" if i == 2 else None
                e.submit("test", {"tile": i}, scope={"group": "R1"}, complete=complete)

            # 2 surviving Phase-0 results + 1 scoped result (the failed tile
            # produces no Phase-0 result).
            results = _wait_for_results(e, "test", 3, timeout=15)
            board = e.status("test")

        assert len(results) == 3
        assert results[-1]["tiles"] == [0, 2]
        assert tally(board)["done"] == 3 and tally(board)["failed"] == 1
        (failed,) = failures(board)
        assert failed["job"] == 2
        assert failed["step"] == "drift_seg"
        assert "bad tile" in failed["error"]

    def test_status_tracks_pending_and_running_jobs(self):
        _temp_step(
            """
            import time
            def run(pd, state, **p):
                time.sleep(0.3)
                return pd
        """,
            name="status_slow",
        )
        yaml = _temp_yaml("wf:\n  - status_slow:")
        from zmart_analysis import Engine

        with Engine(max_concurrent=1) as e:
            e.register("test", yaml, fresh_output())
            e.submit("test", {})
            e.submit("test", {})

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                counts = tally(e.status("test"))
                if counts["running"] == 1 and counts["queued"] == 1:
                    break
                time.sleep(0.01)

            assert counts["queued"] == 1
            assert counts["running"] == 1
            assert counts["done"] == 0
            results = _wait_for_results(e, "test", 2, timeout=5)
            counts = tally(e.status("test"))

        assert len(results) == 2
        assert counts["queued"] == 0
        assert counts["running"] == 0
        assert counts["done"] == 2


# ---- Engine (multi-pipeline) ----------------------------------------


class TestEngineMultiPipeline:
    def test_two_pipelines_shared_workers(self):
        _temp_step(
            """
            def run(pd, state, **p):
                pd["from"] = p.get("from", "unknown")
                return pd
        """,
            name="shared",
        )
        yaml_a = _temp_yaml("wf_a:\n  - shared:\n      from: a")
        yaml_b = _temp_yaml("wf_b:\n  - shared:\n      from: b")
        from zmart_analysis import Engine

        with Engine() as e:
            e.register("a", yaml_a, fresh_output())
            e.register("b", yaml_b, fresh_output())
            e.submit("a", {})
            e.submit("b", {})
            ra = _wait_for_results(e, "a", 1, timeout=10)
            rb = _wait_for_results(e, "b", 1, timeout=10)
        assert len(ra) == 1
        assert len(rb) == 1
        assert ra[0]["from"] == "a"
        assert rb[0]["from"] == "b"


# ---- Engine (deadline) -----------------------------------------------


def _order_step(name):
    """A step that waits at an optional gate, then writes its mark to a log.

    The log is one file the steps of every worker append to, so it shows the
    order the jobs ran in across workers and pipelines.
    """
    return _temp_step(
        """
        import os, time
        def run(pd, state, **p):
            gate = pd["input"].get("gate")
            while gate and not os.path.exists(gate):
                time.sleep(0.01)
            with open(pd["input"]["log"], "a") as f:
                f.write(pd["input"]["mark"] + "\\n")
            return pd
    """,
        name=name,
    )


def _marks(log):
    return Path(log).read_text().split()


class TestEngineDeadline:
    """A submit's deadline orders the jobs that wait."""

    def test_a_nearer_deadline_starts_first(self, tmp_path):
        """No deadline comes last; among equals, the earlier submit."""
        gate, log = tmp_path / "gate", str(tmp_path / "log")
        _order_step("deadline_step")
        yaml = _temp_yaml("wf:\n  - deadline_step:")
        from zmart_analysis import Engine

        with Engine(max_concurrent=1) as e:
            e.register("test", yaml, fresh_output())
            # The blocker takes the only thread and waits at the gate.
            e.submit("test", {"mark": "blocker", "gate": str(gate), "log": log})
            try:
                time.sleep(0.05)
                e.submit("test", {"mark": "none_a", "log": log})
                e.submit("test", {"mark": "far", "log": log}, deadline=600)
                e.submit("test", {"mark": "near", "log": log}, deadline=60)
                e.submit("test", {"mark": "none_b", "log": log})
            finally:
                gate.touch()
            _wait_for_results(e, "test", 5, timeout=10)

        assert _marks(log) == ["blocker", "near", "far", "none_a", "none_b"]

    def test_without_deadlines_jobs_start_in_submission_order_across_pipelines(self, tmp_path):
        """Job ids count per pipeline; the order counts over the engine."""
        gate, log = tmp_path / "gate", str(tmp_path / "log")
        _order_step("across_step")
        yaml = _temp_yaml("wf:\n  - across_step:")
        from zmart_analysis import Engine

        with Engine(max_concurrent=1) as e:
            e.register("one", yaml, fresh_output())
            e.register("two", yaml, fresh_output())
            e.submit("one", {"mark": "blocker", "gate": str(gate), "log": log})
            try:
                time.sleep(0.05)
                e.submit("one", {"mark": "first", "log": log})  # job 2 of "one"
                e.submit("two", {"mark": "second", "log": log})  # job 1 of "two"
            finally:
                gate.touch()
            _wait_for_results(e, "one", 2, timeout=10)
            _wait_for_results(e, "two", 1, timeout=10)

        assert _marks(log) == ["blocker", "first", "second"]

    def test_the_order_holds_while_jobs_wait_for_a_steps_worker(self, tmp_path):
        """Threads to spare, one worker for the step: the nearer deadline
        gets the worker first."""
        gate, log = tmp_path / "gate", str(tmp_path / "log")
        _order_step("one_worker_step")
        yaml = _temp_yaml("wf:\n  - one_worker_step:")
        from zmart_analysis import Engine

        with Engine(max_concurrent=4) as e:
            e.register("test", yaml, fresh_output())
            e.submit("test", {"mark": "blocker", "gate": str(gate), "log": log})
            try:
                time.sleep(0.3)
                e.submit("test", {"mark": "none", "log": log})
                time.sleep(0.3)
                e.submit("test", {"mark": "near", "log": log}, deadline=60)
                time.sleep(0.3)
            finally:
                gate.touch()
            _wait_for_results(e, "test", 3, timeout=10)

        assert _marks(log) == ["blocker", "near", "none"]

    def test_scope_jobs_start_by_deadline(self, tmp_path):
        """The closes of units wait in the same order."""
        gate, log = tmp_path / "gate", str(tmp_path / "log")
        _temp_step(
            """
            def run(pd, state, **p):
                return pd
        """,
            name="unit_image",
        )
        _temp_step(
            """
            import os, time
            def run(pd, state, **p):
                given = pd["parts"][0]["input"]
                gate = given.get("gate")
                while gate and not os.path.exists(gate):
                    time.sleep(0.01)
                with open(given["log"], "a") as f:
                    f.write(given["mark"] + "\\n")
                return {"done": True}
        """,
            name="unit_close",
        )
        yaml = _temp_yaml("""
            wf:
              - unit_image:
              - unit_close:
                  scope: group
        """)
        from zmart_analysis import Engine

        with Engine(max_concurrent=1) as e:
            e.register("test", yaml, fresh_output())
            e.submit(
                "test",
                {"mark": "blocker", "gate": str(gate), "log": log},
                scope={"group": "A"},
                complete="group",
            )
            try:
                time.sleep(0.5)
                e.submit(
                    "test", {"mark": "none", "log": log}, scope={"group": "B"}, complete="group"
                )
                e.submit(
                    "test",
                    {"mark": "near", "log": log},
                    scope={"group": "C"},
                    complete="group",
                    deadline=60,
                )
                time.sleep(0.5)
            finally:
                gate.touch()
            _wait_for_results(e, "test", 6, timeout=20)

        assert _marks(log) == ["blocker", "near", "none"]

    def test_a_job_that_ends_after_its_deadline_is_late(self, tmp_path):
        _temp_step(
            """
            import time
            def run(pd, state, **p):
                time.sleep(0.3)
                return pd
        """,
            name="slow_step",
        )
        yaml = _temp_yaml("wf:\n  - slow_step:")
        output = fresh_output()
        from zmart_analysis import Engine

        with Engine() as e:
            e.register("test", yaml, output)
            late = e.submit("test", {}, deadline=0.05)
            in_time = e.submit("test", {}, deadline=60)
            without = e.submit("test", {})
            board = e.deregister("test")

        assert board[late]["late"] is True
        assert board[in_time]["late"] is False
        assert "deadline" in board[late] and "deadline" in board[in_time]
        assert "deadline" not in board[without] and "late" not in board[without]
        ends = {
            line["job"]: line
            for line in map(json.loads, (Path(output) / "analysis/test/run.jsonl").open())
            if line["event"] == "end"
        }
        assert ends[late]["late"] is True and ends[late]["deadline"]
        assert ends[in_time]["late"] is False
        assert "late" not in ends[without]

    def test_priority_is_gone(self):
        from zmart_analysis import Engine

        with Engine() as e, pytest.raises(TypeError):
            e.submit("test", {}, priority=10)

    def test_a_scope_close_does_not_block_the_image_job_it_waits_for(self):
        """With one thread, the close and its image job still both run."""
        _temp_step(
            """
            def run(pd, state, **p):
                pd["value"] = pd["input"]["value"]
                return pd
        """,
            name="close_input",
        )
        _temp_step(
            """
            def run(pd, state, **p):
                pd["total"] = sum(part["output"]["value"] for part in pd["parts"])
                return pd
        """,
            name="close_collect",
        )
        yaml = _temp_yaml("""
            wf:
              - close_input:
              - close_collect:
                  scope: group
        """)
        from zmart_analysis import Engine

        with Engine(max_concurrent=1) as e:
            e.register("test", yaml, fresh_output())
            e.submit("test", {"value": 7}, scope={"group": "G"}, complete="group")
            results = _wait_for_results(e, "test", 2, timeout=5)

        scoped = [result for result in results if phase_of(result) == 1]
        assert len(results) == 2
        assert len(scoped) == 1
        assert scoped[0]["total"] == 7


# ---- Package API -----------------------------------------------------


class TestPackageAPI:
    def test_public_imports(self):
        import zmart_analysis

        for name in (
            "Engine",
            "WorkerError",
            "WorkerSpawnError",
            "WorkerCrashedError",
            "WorkerTimeoutError",
            "StepExecutionError",
            "ScopeError",
        ):
            assert hasattr(zmart_analysis, name), name

    def test_version(self):
        import zmart_analysis

        assert zmart_analysis.__version__ == "0.6.0rc2"

    def test_engine_in_all(self):
        import zmart_analysis

        assert "Engine" in zmart_analysis.__all__


# ---- The brake: a shutdown that does not wait ---------------------------


def _alive(pid):
    """Whether a process is still running, on either platform."""
    if sys.platform == "win32":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
        ).stdout
        return f'"{pid}"' in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # A killed grandchild whose parent never reaps it stays as a zombie, and
    # a zombie still answers kill(pid, 0). It is dead all the same.
    try:
        with open(f"/proc/{pid}/status") as status:
            for line in status:
                if line.startswith("State:"):
                    return "Z" not in line.split()[1]
    except OSError:
        pass
    return True


def _a_step_that_sleeps(pid_file):
    """A step that writes its own pid, then sleeps longer than any test waits."""
    return _temp_step(f"""
        import os, time
        def run(pd, state, **p):
            open({str(pid_file)!r}, "w").write(str(os.getpid()))
            time.sleep(30)
            return pd
    """)


def _wait_for_file(path, timeout=20):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if os.path.exists(path) and open(path).read().strip():
            return int(open(path).read())
        time.sleep(0.05)
    raise AssertionError(f"{path} never appeared")


class TestTheBrake:
    """The operator's Interrupt must stop a step now, not wait it out.

    Measured on the operator's PC before this: a stop pressed one second
    into a tile test blocked for 19 s and then handed back the field's 447
    objects, because the engine's shutdown joined its threads before it put
    the workers down, and a worker's shutdown terminated only the `conda run`
    wrapper, whose Python grandchild segmented on.
    """

    def test_a_shutdown_now_puts_a_busy_worker_down_at_once(self):
        from zmart_analysis import WorkerCrashedError
        from zmart_analysis.workers import Worker

        pid_file = os.path.join(_TEMP_DIR, f"busy_{_next_id()}.pid")
        path = _a_step_that_sleeps(pid_file)
        w = Worker(environment=None, connect_timeout=10)
        errors = []
        t = threading.Thread(
            target=_capture_exception,
            args=(errors, lambda: w.execute([(path, {})], {}, timeout=60)),
        )
        t.start()
        pid = _wait_for_file(pid_file)
        t0 = time.monotonic()
        w.shutdown(now=True)
        assert time.monotonic() - t0 < 3.0, "a shutdown asked for now must not wait"
        t.join(timeout=5)
        assert not t.is_alive(), "the caller blocked in execute() must be released"
        assert not _alive(pid), "the worker process must be gone"
        assert len(errors) == 1
        assert isinstance(errors[0], WorkerCrashedError)

    def test_a_caller_whose_pipe_is_closed_under_it_is_released(self):
        """On macOS a thread waiting on a pipe is not woken when another thread
        closes it. Seen in CI: shutdown(wait=False) killed the step and closed
        its pipe, and the caller waited in execute() until the test timed out.
        The pipe here models that: closing it wakes no one."""
        from zmart_analysis import WorkerCrashedError
        from zmart_analysis.workers import Worker

        class AWaitThatClosingDoesNotWake:
            def __init__(self, conn):
                self.conn = conn
                self.closed = False

            def send_bytes(self, data):
                self.conn.send_bytes(data)

            def poll(self, timeout=None):
                if self.closed:
                    raise OSError("handle is closed")
                if timeout is None:
                    while True:
                        time.sleep(0.05)
                time.sleep(timeout)
                return False

            def recv_bytes(self):
                return self.conn.recv_bytes()

            def close(self):
                self.closed = True
                self.conn.close()

        pid_file = os.path.join(_TEMP_DIR, f"deaf_{_next_id()}.pid")
        path = _a_step_that_sleeps(pid_file)
        w = Worker(environment=None, connect_timeout=10)
        w.ensure_running()
        w.conn = AWaitThatClosingDoesNotWake(w.conn)
        errors = []
        t = threading.Thread(
            target=_capture_exception,
            args=(errors, lambda: w.execute([(path, {})], {}, timeout=None)),
            daemon=True,
        )
        t.start()
        _wait_for_file(pid_file)
        w.shutdown(now=True)
        t.join(timeout=5)
        assert not t.is_alive(), "the caller blocked in execute() must be released"
        assert len(errors) == 1
        assert isinstance(errors[0], WorkerCrashedError)

    def test_a_shutdown_now_puts_the_whole_tree_down(self):
        """Under a wrapper, as `conda run` is, the grandchild dies too."""
        import zmart_analysis.workers as worker_module
        from zmart_analysis.workers import Worker

        wrapper = _temp_step(
            """
            import subprocess, sys
            # What `conda run -n <env> python` does: start the interpreter as a
            # child and wait for it -- the worker is this process's grandchild.
            raise SystemExit(subprocess.run([sys.executable] + sys.argv[1:]).returncode)
        """,
            name="fake_conda_run",
        )
        pid_file = os.path.join(_TEMP_DIR, f"tree_{_next_id()}.pid")
        path = _a_step_that_sleeps(pid_file)
        real = worker_module.the_python_of
        worker_module.the_python_of = lambda environment: [sys.executable, wrapper]
        try:
            w = Worker(environment="fake-env", connect_timeout=20)
            t = threading.Thread(
                target=_capture_exception,
                args=([], lambda: w.execute([(path, {})], {}, timeout=60)),
            )
            t.start()
            grandchild = _wait_for_file(pid_file)
            wrapper_pid = w.process.pid
            assert grandchild != wrapper_pid
            w.shutdown(now=True)
            t.join(timeout=5)
            time.sleep(0.5)
            assert not _alive(wrapper_pid), "the wrapper must be gone"
            assert not _alive(grandchild), "the worker behind the wrapper must be gone"
        finally:
            worker_module.the_python_of = real

    def test_an_engine_shut_down_without_waiting_stops_its_running_step(self):
        from zmart_analysis import Engine

        pid_file = os.path.join(_TEMP_DIR, f"engine_{_next_id()}.pid")
        _temp_step(
            f"""
            import os, time
            def run(pd, state, **p):
                open({str(pid_file)!r}, "w").write(str(os.getpid()))
                time.sleep(30)
                return pd
        """,
            name="brake_step",
        )
        yaml = _temp_yaml("wf:\n  - brake_step:")
        e = Engine(execution_timeout=None)
        e.register("brake", yaml, fresh_output())
        e.submit("brake", {})
        pid = _wait_for_file(pid_file)
        t0 = time.monotonic()
        e.shutdown(wait=False)
        assert time.monotonic() - t0 < 3.0, (
            "shutting down without waiting must not wait for the step"
        )
        time.sleep(0.5)
        assert not _alive(pid), "the step's worker must be gone"
        # The board goes with the pipeline; that the job ends cancelled, with
        # no output, is test_job_board's deregister-without-waiting case.
        assert e.status() == {}

    def test_a_shutdown_now_during_the_spawn_leaves_nothing_behind(self):
        """A press that lands while the tree is still being built.

        Measured before this: stopping one second into a tile test, while
        `conda run` was still activating, killed the wrapper and left the
        interpreter it started a moment later running -- two orphans after
        every early press.
        """
        import zmart_analysis.workers as worker_module
        from zmart_analysis import WorkerCrashedError, WorkerSpawnError
        from zmart_analysis.workers import Worker

        wrapper = _temp_step(
            """
            import subprocess, sys, time
            # An activation takes its time before the interpreter is started.
            time.sleep(1.5)
            raise SystemExit(subprocess.run([sys.executable] + sys.argv[1:]).returncode)
        """,
            name="slow_conda_run",
        )
        pid_file = os.path.join(_TEMP_DIR, f"spawn_{_next_id()}.pid")
        path = _a_step_that_sleeps(pid_file)
        real = worker_module.the_python_of
        worker_module.the_python_of = lambda environment: [sys.executable, wrapper]
        try:
            w = Worker(environment="slow-env", connect_timeout=20)
            errors = []
            t = threading.Thread(
                target=_capture_exception,
                args=(errors, lambda: w.execute([(path, {})], {}, timeout=60)),
            )
            t.start()
            time.sleep(0.5)
            wrapper_pid = w.process.pid
            w.shutdown(now=True)
            t.join(timeout=10)
            assert not t.is_alive(), "the caller must be released"
            assert len(errors) == 1
            assert isinstance(errors[0], (WorkerCrashedError, WorkerSpawnError))
            time.sleep(3.0)
            assert not _alive(wrapper_pid), "the wrapper must be gone"
            assert not (os.path.exists(pid_file) and _alive(int(open(pid_file).read() or 0))), (
                "an interpreter born after the press must be gone too"
            )
            assert not w.is_alive()
        finally:
            worker_module.the_python_of = real

    def test_a_worker_put_down_before_it_spawned_does_not_spawn(self):
        """A press that lands before the process exists.

        Measured on the operator's PC: a stop one second into a tile test
        came before the worker's spawn, was forgotten, and the whole tree --
        conda, its interpreter, the activation shell and the worker -- came
        up afterwards and stayed, segmenting a field nobody wanted.
        """
        import zmart_analysis.workers as worker_module
        from zmart_analysis import WorkerCrashedError, WorkerSpawnError
        from zmart_analysis.workers import Worker

        pid_file = os.path.join(_TEMP_DIR, f"late_{_next_id()}.pid")
        path = _a_step_that_sleeps(pid_file)
        w = Worker(environment=None, connect_timeout=20)
        may_spawn = threading.Event()
        real_listener = worker_module.Listener
        real_popen = worker_module.subprocess.Popen
        spawned = []

        def listener_after_the_press(*args, **kwargs):
            # The spawn has not begun at all when the press lands: no port,
            # no process -- the worker has nothing yet that a shutdown could
            # touch, and must remember the press instead.
            may_spawn.wait(timeout=10)
            return real_listener(*args, **kwargs)

        def popen_that_remembers(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            spawned.append(process.pid)
            return process

        worker_module.Listener = listener_after_the_press
        worker_module.subprocess.Popen = popen_that_remembers
        try:
            errors = []
            t = threading.Thread(
                target=_capture_exception,
                args=(errors, lambda: w.execute([(path, {})], {}, timeout=60)),
            )
            t.start()
            time.sleep(0.3)
            w.shutdown(now=True)
            may_spawn.set()
            t.join(timeout=15)
            # The real Popen is back before anything else is spawned: the
            # liveness checks below start processes of their own.
            worker_module.subprocess.Popen = real_popen
            worker_module.Listener = real_listener
            assert not t.is_alive(), "the caller must be released"
            assert len(errors) == 1
            assert isinstance(errors[0], (WorkerCrashedError, WorkerSpawnError))
            time.sleep(2.0)
            for pid in spawned:
                assert not _alive(pid), "a process spawned after the press must be put down"
            assert not (os.path.exists(pid_file) and _alive(int(open(pid_file).read() or 0))), (
                "the step must not be running anywhere"
            )
            assert not w.is_alive()
        finally:
            worker_module.subprocess.Popen = real_popen
            worker_module.Listener = real_listener
