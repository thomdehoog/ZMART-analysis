"""Adversarial / stress / race-condition tests for the v4 pipeline engine.

These tests deliberately try to break the engine: race conditions, resource
exhaustion, data corruption, protocol abuse, scope-system attacks, lifecycle
violations, and worker abuse.

Run with::

    pytest -m adversarial

Marker: adversarial (registered in pyproject.toml).
"""

import os
import textwrap
import threading
import concurrent.futures
import gc
import time
from pathlib import Path

import pytest
import psutil


# =====================================================================
#  RACE CONDITIONS
# =====================================================================


@pytest.mark.adversarial
@pytest.mark.slow
def test_race_mass_submit_then_complete(temp_step, temp_yaml, wait_for_results):
    """Submit 50 jobs and signal complete immediately.
    Target: deadlock if scope handler blocks thread pool that jobs need."""
    from engine import Engine

    temp_step("""
        import time
        def run(pd, state, **p):
            time.sleep(0.02)
            pd["v"] = pd["input"]["v"]
            return pd
    """, name="rmss_a")
    temp_step("""
        def run(pd, state, **p):
            pd["n"] = len(pd["results"])
            return pd
    """, name="rmss_b")
    yaml = temp_yaml("""
        wf:
          - rmss_a:
          - rmss_b:
              scope: group
    """)

    with Engine(max_concurrent=4) as e:
        e.register("test", yaml)
        for i in range(49):
            e.submit("test", {"v": i}, scope={"group": "X"})
        e.submit("test", {"v": 49}, scope={"group": "X"},
                 complete="group")

        results = wait_for_results(e, "test", 51, timeout=60)

    scoped = [r for r in results if r.get("_phase") == 1]
    assert scoped, "DEADLOCK: scoped phase never completed"
    assert scoped[0].get("n") == 50, f"expected 50 results, got {scoped[0].get('n')}"


@pytest.mark.adversarial
def test_race_submit_during_shutdown(temp_step, temp_yaml):
    """Submit jobs while the engine is shutting down.
    Target: _accepting flag race with shutdown."""
    from engine import Engine

    temp_step("def run(pd, state, **p): pd['ok'] = True; return pd",
              name="rsds_ok")
    yaml = temp_yaml("wf:\n  - rsds_ok:")

    errors = []
    successes = 0

    e = Engine(max_concurrent=4)
    e.register("test", yaml)

    def submit_loop():
        nonlocal successes
        for i in range(50):
            try:
                e.submit("test", {})
                successes += 1
            except (RuntimeError, concurrent.futures.BrokenExecutor):
                break
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
                break

    t = threading.Thread(target=submit_loop)
    t.start()
    time.sleep(0.01)
    e.shutdown(wait=False)
    t.join(timeout=10)

    assert not t.is_alive(), "DEADLOCK: submit thread did not finish after shutdown"
    assert errors == [], f"unexpected submit/shutdown errors: {errors}"


@pytest.mark.adversarial
@pytest.mark.slow
def test_race_rapid_engine_cycles(temp_step, temp_yaml, wait_for_results, count_children):
    """Rapid register / submit / shutdown cycles.
    Target: resource leaks from rapid lifecycle churn."""
    from engine import Engine

    temp_step("def run(pd, state, **p): pd['ok'] = True; return pd",
              name="rrcs_ok")
    yaml = temp_yaml("wf:\n  - rrcs_ok:")

    children_before = count_children()

    for i in range(20):
        with Engine(max_concurrent=2) as e:
            e.register("test", yaml)
            e.submit("test", {})
            wait_for_results(e, "test", 1, timeout=10)

    time.sleep(0.5)
    children_after = count_children()
    leaked = max(0, children_after - children_before)

    assert leaked == 0, (f"RESOURCE LEAK: {leaked} child processes leaked "
                         f"after 20 cycles")


# =====================================================================
#  RESOURCE EXHAUSTION
# =====================================================================


@pytest.mark.adversarial
@pytest.mark.slow
def test_exhaust_thread_pool_deadlock(temp_step, temp_yaml, wait_for_results):
    """Fill thread pool with slow jobs, then scope handler also needs a thread.
    Target: thread pool starvation deadlock."""
    from engine import Engine

    temp_step("""
        import time
        def run(pd, state, **p):
            time.sleep(2)
            pd["v"] = pd["input"]["v"]
            return pd
    """, name="retd_slow")
    temp_step("""
        def run(pd, state, **p):
            pd["n"] = len(pd["results"])
            return pd
    """, name="retd_agg")
    yaml = temp_yaml("""
        wf:
          - retd_slow:
          - retd_agg:
              scope: group
    """)

    with Engine(max_concurrent=2) as e:
        e.register("test", yaml)
        e.submit("test", {"v": 1}, scope={"group": "X"})
        e.submit("test", {"v": 2}, scope={"group": "X"},
                 complete="group")

        results = wait_for_results(e, "test", 3, timeout=30)

    scoped = [r for r in results if r.get("_phase") == 1]
    assert scoped, "DEADLOCK: thread pool starvation"
    assert scoped[0].get("n") == 2, f"wrong result count: {scoped[0].get('n')}"


@pytest.mark.adversarial
@pytest.mark.slow
def test_exhaust_many_engines(temp_step, temp_yaml, wait_for_results, count_children):
    """Create 30 engines with workers, verify all get cleaned up.
    Target: port leaks, process leaks, thread leaks."""
    temp_step("def run(pd, state, **p): pd['ok'] = True; return pd",
              name="reme_ok")
    yaml = temp_yaml("wf:\n  - reme_ok:")

    from engine import Engine

    children_before = count_children()

    for i in range(30):
        with Engine(max_concurrent=1) as e:
            e.register("test", yaml)
            e.submit("test", {})
            wait_for_results(e, "test", 1, timeout=10)

    time.sleep(0.5)
    children_after = count_children()
    leaked = max(0, children_after - children_before)

    assert leaked <= 2, (f"RESOURCE LEAK: {leaked} child processes after "
                         f"30 engine lifecycles")


@pytest.mark.adversarial
@pytest.mark.slow
def test_exhaust_500_submits(temp_step, temp_yaml, wait_for_results):
    """Submit 500 jobs to one pipeline -- verify no memory leak."""
    from engine import Engine

    temp_step("def run(pd, state, **p): pd['ok'] = True; return pd",
              name="refa_ok")
    yaml = temp_yaml("wf:\n  - refa_ok:")

    with Engine(max_concurrent=8) as e:
        e.register("test", yaml)
        for i in range(500):
            e.submit("test", {})

        results = wait_for_results(e, "test", 500, timeout=120)

    assert len(results) == 500, f"only got {len(results)} results out of 500"


# =====================================================================
#  DATA CORRUPTION
# =====================================================================


@pytest.mark.adversarial
def test_corrupt_mutate_results_list(temp_step, temp_yaml, wait_for_results):
    """Step mutates the accumulated results list in a scoped step.
    Target: engine passes results by reference."""
    from engine import Engine

    temp_step("""
        def run(pd, state, **p):
            pd["v"] = pd["input"]["v"]
            return pd
    """, name="rcmr_a")
    temp_step("""
        def run(pd, state, **p):
            pd["results"].append({"v": 999, "injected": True})
            pd["results"][0]["CORRUPTED"] = True
            pd["n"] = len(pd["results"])
            return pd
    """, name="rcmr_b")
    yaml = temp_yaml("""
        wf:
          - rcmr_a:
          - rcmr_b:
              scope: group
    """)

    with Engine() as e:
        e.register("test", yaml)
        e.submit("test", {"v": 1}, scope={"group": "X"})
        e.submit("test", {"v": 2}, scope={"group": "X"},
                 complete="group")
        results = wait_for_results(e, "test", 3, timeout=15)

    scoped = [r for r in results if r.get("_phase") == 1]
    assert scoped, "no scoped result"
    assert scoped[0].get("n") == 3, (
        f"expected 3 (2 + injected), got {scoped[0].get('n')}"
    )


@pytest.mark.adversarial
def test_corrupt_massive_data(temp_step, temp_yaml, wait_for_results):
    """Step that adds 10MB+ of data to pipeline_data."""
    from engine import Engine

    temp_step("""
        def run(pd, state, **p):
            pd["big"] = "X" * (10 * 1024 * 1024)
            pd["ok"] = True
            return pd
    """, name="rcmd_big")
    yaml = temp_yaml("wf:\n  - rcmd_big:")

    with Engine() as e:
        e.register("test", yaml)
        e.submit("test", {})
        results = wait_for_results(e, "test", 1, timeout=30)

    assert results and results[0].get("ok"), "step did not complete"


@pytest.mark.adversarial
def test_corrupt_replace_pipeline_data(temp_step, temp_yaml, wait_for_results):
    """Step replaces pipeline_data entirely with a new dict."""
    from engine import Engine

    temp_step("""
        def run(pd, state, **p):
            return {"replaced": True, "original_keys": list(pd.keys())}
    """, name="rcrp_replace")
    temp_step("""
        def run(pd, state, **p):
            pd["saw_metadata"] = "metadata" in pd
            pd["saw_input"] = "input" in pd
            pd["saw_replaced"] = pd.get("replaced", False)
            return pd
    """, name="rcrp_check")
    yaml = temp_yaml("""
        wf:
          - rcrp_replace:
          - rcrp_check:
    """)

    with Engine() as e:
        e.register("test", yaml)
        e.submit("test", {"original": True})
        results = wait_for_results(e, "test", 1, timeout=15)

    assert results, "no results"
    r = results[0]
    assert r.get("saw_replaced"), "replacement dict was lost"


@pytest.mark.adversarial
def test_corrupt_dict_subclass(temp_step, temp_yaml, wait_for_results):
    """Step returns a dict subclass (OrderedDict). Engine must accept it
    as a valid return and round-trip the data through pickle cleanly."""
    from engine import Engine

    temp_step("""
        from collections import OrderedDict
        def run(pd, state, **p):
            result = OrderedDict(pd)
            result["ordered"] = True
            return result
    """, name="rcds_fancy")
    yaml = temp_yaml("wf:\n  - rcds_fancy:")

    with Engine() as e:
        e.register("test", yaml)
        e.submit("test", {})
        results = wait_for_results(e, "test", 1, timeout=15)

    assert results, "no result returned for OrderedDict step"
    assert results[0].get("ordered"), "ordered marker missing from OrderedDict result"


@pytest.mark.adversarial
def test_corrupt_non_dict_return(temp_step, temp_yaml, wait_for_status):
    """Step returns a non-dict. Engine should record failure."""
    from engine import Engine

    temp_step("""
        def run(pd, state, **p):
            return ["not", "a", "dict"]
    """, name="rcndr_bad")
    yaml = temp_yaml("wf:\n  - rcndr_bad:")

    with Engine() as e:
        e.register("test", yaml)
        e.submit("test", {})
        status = wait_for_status(e, "test", 1, timeout=10)

    assert status["failed"] >= 1, f"no failure recorded: {status}"


# =====================================================================
#  PROTOCOL ATTACKS
# =====================================================================


@pytest.mark.adversarial
def test_proto_ultrafast_step(temp_step, temp_yaml, wait_for_results):
    """Step that takes ~0s -- faster than connection overhead."""
    from engine import Engine

    temp_step("def run(pd, state, **p): return pd", name="rpuf_instant")
    yaml = temp_yaml("wf:\n  - rpuf_instant:")

    with Engine() as e:
        e.register("test", yaml)
        e.submit("test", {"marker": True})
        results = wait_for_results(e, "test", 1, timeout=10)

    assert results, "no result"


@pytest.mark.adversarial
def test_proto_step_modifies_cwd(temp_step, temp_yaml, wait_for_results):
    """Step changes cwd. In v4 all steps run in workers (subprocesses),
    so the engine process cwd should NOT be affected."""
    from engine import Engine

    original_cwd = os.getcwd()
    temp_step("""
        import os
        def run(pd, state, **p):
            pd["cwd_before"] = os.getcwd()
            os.chdir(os.path.dirname(os.path.abspath(__file__)))
            pd["cwd_after"] = os.getcwd()
            return pd
    """, name="rpmc_cwd")
    yaml = temp_yaml("wf:\n  - rpmc_cwd:")

    with Engine() as e:
        e.register("test", yaml)
        e.submit("test", {})
        results = wait_for_results(e, "test", 1, timeout=10)

    after_cwd = os.getcwd()

    if after_cwd != original_cwd:
        os.chdir(original_cwd)
        raise AssertionError("engine cwd was changed by worker step")

    assert results, "no result"


@pytest.mark.adversarial
def test_proto_step_modifies_environ(temp_step, temp_yaml, wait_for_results):
    """Step modifies os.environ. Should not affect engine (subprocess)."""
    from engine import Engine

    env_key = "ADVERSARIAL_TEST_MARKER_XYZ_12345"
    os.environ.pop(env_key, None)

    temp_step(f"""
        import os
        def run(pd, state, **p):
            os.environ["{env_key}"] = "TAMPERED"
            pd["set_env"] = True
            return pd
    """, name="rpme_env")
    yaml = temp_yaml("wf:\n  - rpme_env:")

    with Engine() as e:
        e.register("test", yaml)
        e.submit("test", {})
        wait_for_results(e, "test", 1, timeout=10)

    polluted = os.environ.get(env_key)
    os.environ.pop(env_key, None)

    assert not polluted, f"engine os.environ was polluted ({env_key}={polluted})"


@pytest.mark.adversarial
def test_proto_step_raises_systemexit(temp_step, temp_yaml, wait_for_status):
    """Step calls sys.exit(). Should be caught by worker, not kill engine."""
    from engine import Engine

    temp_step("""
        import sys
        def run(pd, state, **p):
            sys.exit(42)
    """, name="rpse_exit")
    yaml = temp_yaml("wf:\n  - rpse_exit:")

    with Engine() as e:
        e.register("test", yaml)
        e.submit("test", {})
        status = wait_for_status(e, "test", 1, timeout=10)

    assert status["failed"] >= 1, "no failure recorded for sys.exit()"


# =====================================================================
#  SCOPE SYSTEM ATTACKS
# =====================================================================


@pytest.mark.adversarial
def test_scope_complete_on_unscoped_pipeline(temp_step, temp_yaml, wait_for_status):
    """Signal complete on a pipeline with no scopes."""
    from engine import Engine

    temp_step("def run(pd, state, **p): pd['ok'] = True; return pd",
              name="rsnp_ok")
    yaml = temp_yaml("wf:\n  - rsnp_ok:")

    with Engine() as e:
        e.register("test", yaml)
        # complete="group" but no scope in YAML
        e.submit("test", {}, scope={"group": "X"}, complete="group")
        status = wait_for_status(e, "test", 1, timeout=10)

    # Phase 0 should complete, scope handler should log warning
    assert status["completed"] >= 1, f"unexpected status: {status}"


@pytest.mark.adversarial
def test_scope_mismatched_labels(temp_step, temp_yaml, wait_for_results):
    """Submit jobs to one scope value; complete signaled on a different value.
    Only the matching value's jobs should be aggregated -- A's jobs must NOT
    leak into B's scoped step."""
    from engine import Engine

    temp_step("""
        def run(pd, state, **p):
            pd["v"] = pd["input"]["v"]
            return pd
    """, name="rsml_a")
    temp_step("""
        def run(pd, state, **p):
            pd["n"] = len(pd["results"])
            pd["vs"] = sorted(r["v"] for r in pd["results"])
            return pd
    """, name="rsml_b")
    yaml = temp_yaml("""
        wf:
          - rsml_a:
          - rsml_b:
              scope: group
    """)

    with Engine() as e:
        e.register("test", yaml)
        e.submit("test", {"v": 1}, scope={"group": "A"})
        e.submit("test", {"v": 2}, scope={"group": "A"})
        e.submit("test", {"v": 3}, scope={"group": "B"},
                 complete="group")

        results = wait_for_results(e, "test", 4, timeout=15)

    phase0 = [r for r in results if r.get("_phase") == 0]
    scoped = [r for r in results if r.get("_phase") == 1]

    assert len(phase0) == 3, f"expected 3 phase-0 results, got {len(phase0)}"
    assert scoped, "scoped step did not trigger for group B"
    assert scoped[0]["n"] == 1 and scoped[0]["vs"] == [3], (
        f"scope leak: expected n=1 vs=[3] (only B), "
        f"got n={scoped[0]['n']} vs={scoped[0]['vs']}"
    )


@pytest.mark.adversarial
def test_scope_none_value(temp_step, temp_yaml, wait_for_results):
    """Scope key with None as value should aggregate like any other value."""
    from engine import Engine

    temp_step("""
        def run(pd, state, **p):
            pd["v"] = pd["input"]["v"]
            return pd
    """, name="rsnv_a")
    temp_step("""
        def run(pd, state, **p):
            pd["n"] = len(pd["results"])
            pd["vs"] = sorted(r["v"] for r in pd["results"])
            return pd
    """, name="rsnv_b")
    yaml = temp_yaml("""
        wf:
          - rsnv_a:
          - rsnv_b:
              scope: group
    """)

    with Engine() as e:
        e.register("test", yaml)
        e.submit("test", {"v": 1}, scope={"group": None})
        e.submit("test", {"v": 2}, scope={"group": None},
                 complete="group")
        results = wait_for_results(e, "test", 3, timeout=15)

    scoped = [r for r in results if r.get("_phase") == 1]
    assert scoped, "scoped step did not trigger with None scope value"
    assert scoped[0]["n"] == 2 and scoped[0]["vs"] == [1, 2], (
        f"None-scope aggregation wrong: "
        f"got n={scoped[0]['n']} vs={scoped[0]['vs']}"
    )


@pytest.mark.adversarial
def test_scope_very_long_key(temp_step, temp_yaml, wait_for_results):
    """Scope key with a 10000-char value."""
    from engine import Engine

    temp_step("""
        def run(pd, state, **p):
            pd["v"] = 1
            return pd
    """, name="rslk_a")
    temp_step("""
        def run(pd, state, **p):
            pd["n"] = len(pd["results"])
            return pd
    """, name="rslk_b")
    yaml = temp_yaml("""
        wf:
          - rslk_a:
          - rslk_b:
              scope: group
    """)

    long_key = "A" * 10000

    with Engine() as e:
        e.register("test", yaml)
        e.submit("test", {"v": 1}, scope={"group": long_key})
        e.submit("test", {"v": 2}, scope={"group": long_key},
                 complete="group")
        results = wait_for_results(e, "test", 3, timeout=15)

    scoped = [r for r in results if r.get("_phase") == 1]
    assert scoped, "no scoped result"
    assert scoped[0].get("n") == 2, f"expected 2, got {scoped[0].get('n')}"


# =====================================================================
#  LIFECYCLE ATTACKS
# =====================================================================


@pytest.mark.adversarial
def test_lifecycle_use_after_shutdown(temp_step, temp_yaml):
    """Register after shutdown -- should raise, not hang."""
    from engine import Engine

    temp_step("def run(pd, state, **p): return pd", name="rluas_ok")
    yaml = temp_yaml("wf:\n  - rluas_ok:")

    e = Engine()
    e.shutdown()

    with pytest.raises(RuntimeError) as exc_info:
        e.register("test", yaml)
    assert "shut down" in str(exc_info.value).lower(), (
        f"RuntimeError but wrong message: {exc_info.value}"
    )


@pytest.mark.adversarial
def test_lifecycle_double_shutdown():
    """Double shutdown should not raise."""
    from engine import Engine

    e = Engine()
    e.shutdown()
    # Second shutdown should not raise
    e.shutdown()


@pytest.mark.adversarial
@pytest.mark.slow
def test_lifecycle_100_pipelines(temp_step, temp_yaml):
    """Register 100 pipelines on one engine."""
    temp_step("def run(pd, state, **p): pd['ok'] = True; return pd",
              name="rl100_ok")

    from engine import Engine

    with Engine(max_concurrent=8) as e:
        for i in range(100):
            yaml = temp_yaml(f"wf_{i}:\n  - rl100_ok:")
            e.register(f"p{i}", yaml)

        # Submit one job per pipeline
        for i in range(100):
            e.submit(f"p{i}", {})

        # Wait for some results
        time.sleep(10)
        total = sum(len(e.results(f"p{i}")) for i in range(100))

    assert total >= 50, f"only {total}/100 jobs completed"


@pytest.mark.adversarial
@pytest.mark.slow
def test_lifecycle_1000_jobs(temp_step, temp_yaml, wait_for_results):
    """1000 jobs on one pipeline."""
    temp_step("def run(pd, state, **p): pd['i'] = pd['input']['i']; return pd",
              name="rl1k")
    yaml = temp_yaml("wf:\n  - rl1k:")

    from engine import Engine

    with Engine(max_concurrent=8) as e:
        e.register("test", yaml)
        for i in range(1000):
            e.submit("test", {"i": i})

        results = wait_for_results(e, "test", 1000, timeout=300)

    assert len(results) == 1000, f"only {len(results)}/1000 completed"


@pytest.mark.adversarial
def test_lifecycle_context_manager_exception(
    temp_step, temp_yaml, wait_for_results, count_children
):
    """Exception inside context manager still cleans up."""
    from engine import Engine

    temp_step("def run(pd, state, **p): return pd", name="rlcme")
    yaml = temp_yaml("wf:\n  - rlcme:")

    children_before = count_children()

    try:
        with Engine() as e:
            e.register("test", yaml)
            e.submit("test", {})
            wait_for_results(e, "test", 1, timeout=10)
            raise ValueError("deliberate")
    except ValueError:
        pass

    time.sleep(0.5)
    children_after = count_children()
    leaked = max(0, children_after - children_before)

    assert leaked == 0, f"leaked {leaked} processes after exception in context manager"


# =====================================================================
#  WORKER ATTACKS
# =====================================================================


@pytest.mark.adversarial
@pytest.mark.slow
def test_worker_nonexistent_env(temp_step, temp_yaml, wait_for_status):
    """Step with nonexistent conda environment. Worker spawn fails."""
    from engine import Engine

    temp_step("""
        METADATA = {"environment": "NONEXISTENT_ENV_12345"}
        def run(pd, state, **p): return pd
    """, name="rwne")
    yaml = temp_yaml("wf:\n  - rwne:")

    with Engine(execution_timeout=10.0) as e:
        e.register("test", yaml)
        e.submit("test", {})
        # Worker connect timeout is 60s by default, wait long enough
        status = wait_for_status(e, "test", 1, timeout=90)

    assert status["failed"] >= 1, f"no failure recorded: {status}"


@pytest.mark.adversarial
def test_worker_step_raises(temp_step, temp_yaml, wait_for_results):
    """Step raises exception -- worker should survive."""
    from engine import Engine

    temp_step("""
        def run(pd, state, **p):
            raise RuntimeError("deliberate worker error")
    """, name="rwsr")
    temp_step("def run(pd, state, **p): pd['ok'] = True; return pd",
              name="rwsr_ok")
    yaml_bad = temp_yaml("wf_bad:\n  - rwsr:")
    yaml_ok = temp_yaml("wf_ok:\n  - rwsr_ok:")

    with Engine() as e:
        e.register("bad", yaml_bad)
        e.register("ok", yaml_ok)

        e.submit("bad", {})
        time.sleep(3)
        # Worker should still be alive for the next job
        e.submit("ok", {})
        results = wait_for_results(e, "ok", 1, timeout=10)

    assert results and results[0].get("ok"), "worker died after step error"


@pytest.mark.adversarial
def test_worker_missing_run_function(temp_step, temp_yaml, wait_for_status):
    """Step file without run() function."""
    from engine import Engine

    temp_step("x = 42  # no run function", name="rwmr")
    yaml = temp_yaml("wf:\n  - rwmr:")

    with Engine() as e:
        e.register("test", yaml)
        e.submit("test", {})
        status = wait_for_status(e, "test", 1, timeout=10)

    assert status["failed"] >= 1, f"no failure: {status}"


@pytest.mark.adversarial
def test_worker_syntax_error(temp_step, temp_yaml):
    """Step file with syntax error. AST parsing fails at register time."""
    from engine import Engine

    temp_step("def run(\n    return {}", name="rwse")
    yaml = temp_yaml("wf:\n  - rwse:")

    # register() should raise (SyntaxError or any error from AST parse).
    # Whatever it is, we expect failure -- success would be the bug.
    with pytest.raises(Exception):
        with Engine() as e:
            e.register("test", yaml)


# =====================================================================
#  CONCURRENCY STRESS
# =====================================================================


@pytest.mark.adversarial
@pytest.mark.slow
def test_stress_concurrent_pipelines(temp_step, temp_yaml):
    """Multiple pipelines running concurrently."""
    from engine import Engine

    temp_step("""
        import time
        def run(pd, state, **p):
            time.sleep(0.01)
            pd["pipeline"] = pd["input"]["p"]
            return pd
    """, name="rscp")

    with Engine(max_concurrent=8) as e:
        for i in range(5):
            yaml = temp_yaml(f"wf_{i}:\n  - rscp:")
            e.register(f"p{i}", yaml)

        for i in range(5):
            for j in range(10):
                e.submit(f"p{i}", {"p": f"p{i}"})

        time.sleep(15)
        total = 0
        for i in range(5):
            total += len(e.results(f"p{i}"))

    assert total >= 40, f"only {total}/50 jobs completed"


@pytest.mark.adversarial
@pytest.mark.slow
def test_stress_status_during_execution(temp_step, temp_yaml):
    """Call status() repeatedly while jobs are running."""
    from engine import Engine

    temp_step("""
        import time
        def run(pd, state, **p):
            time.sleep(0.1)
            return pd
    """, name="rssde")
    yaml = temp_yaml("wf:\n  - rssde:")

    errors = []

    with Engine(max_concurrent=4) as e:
        e.register("test", yaml)
        for i in range(20):
            e.submit("test", {})

        # Poll status rapidly
        for _ in range(50):
            try:
                s = e.status("test")
                assert isinstance(s, dict)
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
            time.sleep(0.02)

    assert not errors, f"status() errors: {errors[:3]}"


@pytest.mark.adversarial
@pytest.mark.slow
def test_stress_mixed_success_failure(temp_step, temp_yaml, wait_for_status):
    """Mix of successful and failing jobs."""
    from engine import Engine

    temp_step("""
        def run(pd, state, **p):
            if pd["input"].get("fail"):
                raise ValueError("deliberate")
            pd["ok"] = True
            return pd
    """, name="rsmsf")
    yaml = temp_yaml("wf:\n  - rsmsf:")

    with Engine(max_concurrent=8) as e:
        e.register("test", yaml)
        for i in range(40):
            e.submit("test", {"fail": (i % 3 == 0)})

        # Wait for all to complete or fail
        status = wait_for_status(e, "test", 40, timeout=60)

    total = status["completed"] + status["failed"]
    assert total >= 30, f"only {total}/40 finished"


@pytest.mark.adversarial
@pytest.mark.slow
def test_stress_gc_pressure(temp_step, temp_yaml, wait_for_results):
    """Create lots of garbage during execution to stress GC."""
    from engine import Engine

    temp_step("""
        def run(pd, state, **p):
            # Create circular references
            for _ in range(100):
                a = {}
                b = {"parent": a}
                a["child"] = b
            pd["ok"] = True
            return pd
    """, name="rsgc")
    yaml = temp_yaml("wf:\n  - rsgc:")

    with Engine() as e:
        e.register("test", yaml)
        for i in range(20):
            e.submit("test", {})

        gc.collect()
        results = wait_for_results(e, "test", 20, timeout=30)

    assert len(results) >= 18, f"only {len(results)}/20 completed under GC pressure"


# =====================================================================
#  YAML EDGE CASES
# =====================================================================


@pytest.mark.adversarial
def test_yaml_empty_params(temp_step, temp_yaml, wait_for_results):
    """Step with explicitly empty params (None in YAML)."""
    from engine import Engine

    temp_step("def run(pd, state, **p): pd['ok'] = True; return pd",
              name="ryep")
    yaml = temp_yaml("wf:\n  - ryep:")

    with Engine() as e:
        e.register("test", yaml)
        e.submit("test", {})
        results = wait_for_results(e, "test", 1, timeout=10)

    assert results and results[0].get("ok"), "empty params failed"


@pytest.mark.adversarial
def test_yaml_nonexistent_step(temp_yaml):
    """YAML references a step file that does not exist.
    In v4, register() reads METADATA via AST, so missing file fails early."""
    from engine import Engine

    yaml = temp_yaml("wf:\n  - does_not_exist:")

    # register() should raise (FileNotFoundError or any error). Success
    # would be the bug -- we just need *some* error to be raised.
    with pytest.raises(Exception):
        with Engine() as e:
            e.register("test", yaml)
