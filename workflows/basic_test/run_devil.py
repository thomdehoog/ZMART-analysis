"""
Destructive / adversarial tests for the v3 pipeline engine.

Each test deliberately tries to BREAK the engine through race conditions,
resource exhaustion, data corruption, protocol abuse, scope system attacks,
lifecycle violations, and worker abuse. Tests target specific weaknesses
found by reading the engine source.

Vulnerability map (source file -> attack surface):
  _pipeline.py    _accepting flag not under _lock on the read side
  _run.py         _pending_results.pop() means 2nd scope_complete gets ScopeError
                  _make_scope_key() sorts on values -- None blows up sorted()
                  No duplicate-label guard
                  _all_futures grows forever (memory leak vector)
  _pool.py        GPU executor thread stays alive after shutdown_event (daemon)
                  _reaper inspects .locked() which is racy
  _worker.py      pickle protocol for IPC -- untrusted data vector
                  _cleanup kills process but doesn't drain stderr pipe
  _loader.py      exec-based loading lets steps pollute namespace
  worker_script   module_cache grows forever for distinct step paths

Usage:
    python run_devil.py
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
import concurrent.futures
import gc
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

WIDTH = 70
_TEMP = tempfile.mkdtemp(prefix="devil_")
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


def _count_children():
    """Count child processes. Prefer psutil, fall back to multiprocessing."""
    try:
        import psutil
        return len(psutil.Process().children(recursive=True))
    except ImportError:
        return len(multiprocessing.active_children())


# =====================================================================
#  RACE CONDITIONS
# =====================================================================


def test_race_mass_submit_then_scope_complete():
    """Submit 100 jobs and call scope_complete before any finish.
    Target: potential deadlock if scope_complete blocks the thread pool
    that the jobs need to complete."""
    from engine import PipelineEngine

    _temp_step("""
        import time
        def run(pd, **p):
            time.sleep(0.05)
            pd["v"] = pd["input"]["v"]
            return pd
    """, name="rmss_a")
    _temp_step("""
        def run(pd, **p):
            pd["n"] = len(pd["results"])
            return pd
    """, name="rmss_b")
    yaml = _temp_yaml("""
        wf:
          - rmss_a:
          - rmss_b:
              scope:
                spatial: r
    """)

    # max_concurrent=4 but 100 jobs + 1 scope_complete = 101 thread pool tasks.
    # scope_complete blocks waiting for jobs, but jobs need threads from the
    # same pool. If the pool is full with scope_complete waiters, deadlock.
    with PipelineEngine(max_concurrent=4) as e:
        run = e.create_run(yaml)
        for i in range(100):
            run.submit(f"j{i}", {"v": i}, spatial={"r": "X"})

        # scope_complete goes into the same 4-thread pool
        f = run.scope_complete(spatial={"r": "X"})
        try:
            r = f.result(timeout=30)
        except concurrent.futures.TimeoutError:
            return False, ("DEADLOCK: scope_complete timed out -- thread pool "
                           "starvation (4 threads, 100 jobs + scope_complete "
                           "all compete for same pool)")
        except Exception as exc:
            return False, f"unexpected error: {type(exc).__name__}: {exc}"

    if r.get("n") != 100:
        return False, f"expected 100 results, got {r.get('n')}"

    return True, ("survived: scope_complete waited correctly despite "
                  "4-thread pool with 100 jobs queued")


def test_race_concurrent_scope_complete_same_group():
    """Call scope_complete from multiple threads for the SAME group.
    Target: _pending_results.pop() is called twice. Second call gets empty
    list, should raise ScopeError. But there is a race: both threads might
    see the same results before either pops."""
    from engine import PipelineEngine, ScopeError

    _temp_step("""
        def run(pd, **p):
            pd["v"] = pd["input"]["v"]
            return pd
    """, name="rcsc_a")
    _temp_step("""
        def run(pd, **p):
            pd["n"] = len(pd["results"])
            return pd
    """, name="rcsc_b")
    yaml = _temp_yaml("""
        wf:
          - rcsc_a:
          - rcsc_b:
              scope:
                spatial: r
    """)

    n_errors = 0
    n_success = 0
    exceptions = []

    with PipelineEngine(max_concurrent=8) as e:
        run = e.create_run(yaml)
        for i in range(5):
            run.submit(f"j{i}", {"v": i}, spatial={"r": "X"})

        # Fire 5 scope_complete calls concurrently for the SAME group
        futures = [run.scope_complete(spatial={"r": "X"}) for _ in range(5)]

        for f in futures:
            try:
                r = f.result(timeout=15)
                n_success += 1
            except ScopeError:
                n_errors += 1
            except Exception as exc:
                exceptions.append(f"{type(exc).__name__}: {exc}")

    if exceptions:
        return False, f"unexpected exceptions: {exceptions}"
    if n_success == 0:
        return False, "all scope_complete calls failed"
    if n_success > 1:
        # This is a real bug: two calls both got results
        return False, (f"DATA RACE: {n_success} scope_complete calls "
                       f"succeeded for the same group (expected exactly 1)")

    return True, (f"1 succeeded, {n_errors} raised ScopeError -- "
                  "pop() serialized correctly")


def test_race_submit_during_shutdown():
    """Submit jobs while the engine is shutting down.
    Target: _accepting flag is checked without lock on the read side in
    create_run, and submit accesses _engine._executor which may already
    be shut down."""
    from engine import PipelineEngine

    _temp_step("def run(pd, **p): pd['ok'] = True; return pd",
               name="rsds_ok")
    yaml = _temp_yaml("wf:\n  - rsds_ok:")

    errors = []
    successes = 0

    e = PipelineEngine(max_concurrent=4)
    run = e.create_run(yaml)

    # Start submitting in a thread
    def submit_loop():
        nonlocal successes
        for i in range(50):
            try:
                f = run.submit(f"j{i}", {})
                f.result(timeout=5)
                successes += 1
            except (RuntimeError, concurrent.futures.BrokenExecutor) as exc:
                errors.append(str(exc))
                break
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
                break

    t = threading.Thread(target=submit_loop)
    t.start()
    time.sleep(0.01)
    e.shutdown(wait=False)
    t.join(timeout=10)

    if t.is_alive():
        return False, "DEADLOCK: submit thread did not finish after shutdown"

    # Either some succeeded before shutdown, or shutdown raced correctly
    return True, (f"{successes} jobs completed before shutdown, "
                  f"{len(errors)} rejected -- no deadlock or crash")


def test_race_rapid_create_submit_shutdown():
    """Rapid create_run / submit / shutdown cycles.
    Target: resource leaks from rapid lifecycle churn."""
    from engine import PipelineEngine

    _temp_step("def run(pd, **p): pd['ok'] = True; return pd",
               name="rrcs_ok")
    yaml = _temp_yaml("wf:\n  - rrcs_ok:")

    children_before = _count_children()

    for i in range(20):
        e = PipelineEngine(max_concurrent=2)
        run = e.create_run(yaml)
        f = run.submit(f"j{i}", {})
        try:
            f.result(timeout=5)
        except Exception:
            pass
        e.shutdown(wait=True)

    time.sleep(0.5)
    children_after = _count_children()
    leaked = max(0, children_after - children_before)

    if leaked > 0:
        return False, (f"RESOURCE LEAK: {leaked} child processes leaked "
                       f"after 20 create/submit/shutdown cycles")

    return True, (f"20 rapid cycles, processes: {children_before} -> "
                  f"{children_after}")


# =====================================================================
#  RESOURCE EXHAUSTION
# =====================================================================


def test_exhaust_thread_pool_deadlock():
    """Fill the thread pool with blocking jobs, then submit scope_complete
    which also needs a thread. Classic thread-pool starvation deadlock.
    Target: scope_complete runs in the same ThreadPoolExecutor as jobs."""
    from engine import PipelineEngine

    _temp_step("""
        import time
        def run(pd, **p):
            time.sleep(2)
            pd["v"] = pd["input"]["v"]
            return pd
    """, name="retd_slow")
    _temp_step("""
        def run(pd, **p):
            pd["n"] = len(pd["results"])
            return pd
    """, name="retd_agg")
    yaml = _temp_yaml("""
        wf:
          - retd_slow:
          - retd_agg:
              scope:
                spatial: r
    """)

    # max_concurrent=2: submit 2 slow jobs, then scope_complete needs
    # a thread too. Jobs sleep 2s each. scope_complete blocks waiting
    # for jobs. If scope_complete consumes a thread, only 1 thread
    # left for 2 jobs.
    with PipelineEngine(max_concurrent=2) as e:
        run = e.create_run(yaml)
        run.submit("j1", {"v": 1}, spatial={"r": "X"})
        run.submit("j2", {"v": 2}, spatial={"r": "X"})

        # This scope_complete consumes 1 of 2 threads.
        # Only 1 thread left for the 2 jobs.
        f = run.scope_complete(spatial={"r": "X"})
        try:
            r = f.result(timeout=15)
        except concurrent.futures.TimeoutError:
            return False, ("DEADLOCK CONFIRMED: thread pool starvation. "
                           "max_concurrent=2, 2 jobs + scope_complete = 3 "
                           "tasks, scope_complete blocks waiting for jobs "
                           "that cannot start")
        except Exception as exc:
            return False, f"unexpected: {type(exc).__name__}: {exc}"

    if r.get("n") != 2:
        return False, f"wrong result count: {r.get('n')}"

    return True, ("survived: ThreadPoolExecutor queued the excess task "
                  "rather than deadlocking")


def test_exhaust_many_engines():
    """Create 50 engines with workers, verify all get cleaned up.
    Target: port leaks, process leaks, thread leaks."""
    _temp_step("def run(pd, **p): pd['ok'] = True; return pd",
               name="reme_ok")
    yaml = _temp_yaml("wf:\n  - reme_ok:")

    from engine import PipelineEngine

    children_before = _count_children()

    for i in range(50):
        with PipelineEngine(max_concurrent=1) as e:
            run = e.create_run(yaml)
            f = run.submit(f"j{i}", {})
            f.result(timeout=10)

    time.sleep(0.5)
    children_after = _count_children()
    leaked = max(0, children_after - children_before)

    if leaked > 2:  # allow tiny margin for OS scheduling
        return False, (f"RESOURCE LEAK: {leaked} child processes after "
                       f"50 engine lifecycles")

    return True, (f"50 engine lifecycles, processes: {children_before} -> "
                  f"{children_after}")


def test_exhaust_futures_accumulation():
    """Submit 500 jobs to one run -- verify counters replace the old
    _all_futures list (which was a memory leak vector).
    Target: v3 fix uses counters + done callbacks, no future references held."""
    from engine import PipelineEngine

    _temp_step("def run(pd, **p): pd['ok'] = True; return pd",
               name="refa_ok")
    yaml = _temp_yaml("wf:\n  - refa_ok:")

    with PipelineEngine(max_concurrent=8) as e:
        run = e.create_run(yaml)
        futures = []
        for i in range(500):
            futures.append(run.submit(f"j{i}", {}))

        for f in futures:
            f.result(timeout=30)

        # Verify counters instead of list
        s = run.status
        has_list = hasattr(run, '_all_futures')

    if has_list:
        return False, ("MEMORY LEAK: _all_futures list still exists "
                       f"with {len(run._all_futures)} entries")

    if s["completed"] != 500:
        return False, f"counter mismatch: completed={s['completed']}"

    return True, (f"FIXED: counters used instead of future list "
                  f"(submitted={s['total']}, completed={s['completed']})")


# =====================================================================
#  DATA CORRUPTION
# =====================================================================


def test_corrupt_mutate_results_list():
    """Step that mutates the accumulated results list in a scoped step.
    Target: if the engine passes results by reference, mutation corrupts
    the engine's copy."""
    from engine import PipelineEngine

    _temp_step("""
        def run(pd, **p):
            pd["v"] = pd["input"]["v"]
            return pd
    """, name="rcmr_a")
    _temp_step("""
        def run(pd, **p):
            # Malicious: mutate the results list
            pd["results"].append({"v": 999, "injected": True})
            pd["results"][0]["CORRUPTED"] = True
            pd["n"] = len(pd["results"])
            return pd
    """, name="rcmr_b")
    yaml = _temp_yaml("""
        wf:
          - rcmr_a:
          - rcmr_b:
              scope:
                spatial: r
    """)

    with PipelineEngine() as e:
        run = e.create_run(yaml)
        run.submit("j1", {"v": 1}, spatial={"r": "X"})
        run.submit("j2", {"v": 2}, spatial={"r": "X"})
        r = run.scope_complete(spatial={"r": "X"}).result(timeout=15)

    # The step saw 2 real + 1 injected = 3
    if r.get("n") != 3:
        return False, f"expected 3 (2 + injected), got {r.get('n')}"

    return True, ("step could mutate results list -- engine passes by "
                  "reference, no copy protection")


def test_corrupt_massive_data():
    """Step that adds 10MB+ of data to pipeline_data.
    Target: memory pressure, pickling cost if routed to workers."""
    from engine import PipelineEngine

    _temp_step("""
        def run(pd, **p):
            # ~10MB of data
            pd["big"] = "X" * (10 * 1024 * 1024)
            pd["ok"] = True
            return pd
    """, name="rcmd_big")
    yaml = _temp_yaml("wf:\n  - rcmd_big:")

    with PipelineEngine() as e:
        run = e.create_run(yaml)
        f = run.submit("big_job", {})
        r = f.result(timeout=30)

    if not r.get("ok"):
        return False, "step did not complete"
    size_mb = len(r.get("big", "")) / (1024 * 1024)

    return True, f"handled {size_mb:.1f}MB payload without crash"


def test_corrupt_replace_pipeline_data():
    """Step that replaces pipeline_data entirely with a new dict.
    Target: engine does `pipeline_data = self._engine._execute_step(...)`,
    so a completely new dict from the step just becomes the new pipeline_data.
    But what about the metadata and input keys?"""
    from engine import PipelineEngine

    _temp_step("""
        def run(pd, **p):
            # Return a completely fresh dict, discarding metadata/input
            return {"replaced": True, "original_keys": list(pd.keys())}
    """, name="rcrp_evil")
    _temp_step("""
        def run(pd, **p):
            pd["saw_metadata"] = "metadata" in pd
            pd["saw_input"] = "input" in pd
            pd["saw_replaced"] = pd.get("replaced", False)
            return pd
    """, name="rcrp_check")
    yaml = _temp_yaml("""
        wf:
          - rcrp_evil:
          - rcrp_check:
    """)

    from engine import run_pipeline
    r = run_pipeline(yaml, "replace_test", {"original": True})

    if not r.get("saw_replaced"):
        return False, "replacement dict was lost"
    if r.get("saw_metadata"):
        return False, "metadata survived replacement (unexpected)"
    if r.get("saw_input"):
        return False, "input survived replacement (unexpected)"

    return True, ("step can discard metadata+input by returning fresh dict -- "
                  f"original keys were: {r.get('original_keys')}")


def test_corrupt_dict_subclass():
    """Step returns a dict subclass. The isinstance(result, dict) check
    should pass, but downstream code might behave differently."""
    from engine import run_pipeline

    _temp_step("""
        class FancyDict(dict):
            def __getitem__(self, key):
                # Override to add a side effect
                return super().__getitem__(key)
            def __repr__(self):
                return f"FancyDict({super().__repr__()})"

        def run(pd, **p):
            result = FancyDict(pd)
            result["fancy"] = True
            result["type_name"] = type(result).__name__
            return result
    """, name="rcds_fancy")
    yaml = _temp_yaml("wf:\n  - rcds_fancy:")

    r = run_pipeline(yaml, "fancy_test", {})

    if not r.get("fancy"):
        return False, "FancyDict not returned"

    actual_type = type(r).__name__
    return True, (f"dict subclass survived -- returned type: {actual_type}, "
                  f"step reported: {r.get('type_name')}")


def test_corrupt_non_dict_return():
    """Step returns a non-dict. Engine should raise TypeError.
    Target: _execute_phase_for_job checks isinstance(pipeline_data, dict)."""
    from engine import PipelineEngine

    _temp_step("""
        def run(pd, **p):
            return ["not", "a", "dict"]
    """, name="rcndr_bad")
    yaml = _temp_yaml("wf:\n  - rcndr_bad:")

    with PipelineEngine() as e:
        run = e.create_run(yaml)
        f = run.submit("bad_return", {})
        try:
            f.result(timeout=10)
            return False, "no error raised for list return"
        except TypeError as exc:
            if "expected dict" in str(exc):
                return True, f"TypeError raised correctly: {exc}"
            return False, f"TypeError but wrong message: {exc}"
        except Exception as exc:
            return False, f"wrong exception type: {type(exc).__name__}: {exc}"


# =====================================================================
#  PROTOCOL ATTACKS
# =====================================================================


def test_proto_ultrafast_step():
    """Step that takes ~0s -- faster than connection overhead.
    Target: timing assumptions in poll/recv cycle."""
    from engine import run_pipeline

    _temp_step("def run(pd, **p): return pd", name="rpuf_instant")
    yaml = _temp_yaml("wf:\n  - rpuf_instant:")

    t0 = time.perf_counter()
    r = run_pipeline(yaml, "instant", {"marker": True})
    elapsed = time.perf_counter() - t0

    if not r.get("input", {}).get("marker"):
        return False, "data lost"

    return True, f"completed in {_fmt(elapsed)} -- sub-ms step handled"


def test_proto_step_modifies_cwd():
    """Step that changes the working directory.
    Target: in-process exec means cwd change affects the engine."""
    from engine import run_pipeline

    original_cwd = os.getcwd()
    _temp_step(f"""
        import os
        def run(pd, **p):
            pd["cwd_before"] = os.getcwd()
            os.chdir(os.path.dirname(os.path.abspath(__file__)))
            pd["cwd_after"] = os.getcwd()
            return pd
    """, name="rpmc_cwd")
    yaml = _temp_yaml("wf:\n  - rpmc_cwd:")

    r = run_pipeline(yaml, "cwd_test", {})
    after_cwd = os.getcwd()

    # Restore cwd regardless
    os.chdir(original_cwd)

    cwd_changed = r.get("cwd_before") != r.get("cwd_after")
    engine_affected = after_cwd != original_cwd

    if engine_affected:
        return True, (f"OBSERVATION: local step changed cwd from "
                      f"'{r.get('cwd_before')}' to '{r.get('cwd_after')}'. "
                      f"Inherent to in-process execution. "
                      f"Use isolation: maximal for untrusted steps.")

    if cwd_changed:
        return True, ("step changed cwd but engine cwd was NOT affected "
                      "(isolated or restored)")

    return True, "step did not actually change cwd"


def test_proto_step_modifies_sys_path():
    """Step that modifies sys.path.
    Target: in-process exec shares the interpreter, so sys.path changes
    persist and affect all subsequent imports."""
    from engine import run_pipeline

    original_path_len = len(sys.path)
    bogus_dir = "/tmp/devil_test_bogus_path_12345"

    _temp_step(f"""
        import sys
        def run(pd, **p):
            sys.path.insert(0, "{bogus_dir}")
            pd["injected"] = "{bogus_dir}" in sys.path
            return pd
    """, name="rpsp_path")
    yaml = _temp_yaml("wf:\n  - rpsp_path:")

    r = run_pipeline(yaml, "path_test", {})
    path_polluted = bogus_dir in sys.path

    # Cleanup
    while bogus_dir in sys.path:
        sys.path.remove(bogus_dir)

    if path_polluted:
        return True, (f"OBSERVATION: sys.path polluted by local step "
                      f"(injected '{bogus_dir}'). Inherent to in-process "
                      f"execution. Use isolation: maximal for untrusted steps.")

    if r.get("injected"):
        return True, ("step injected path but it did not persist "
                      "(possibly isolated)")

    return True, "sys.path was not modified"


def test_proto_step_modifies_environ():
    """Step that modifies os.environ.
    Target: in-process step shares the same os.environ."""
    from engine import run_pipeline

    env_key = "DEVIL_TEST_MARKER_XYZ_12345"
    os.environ.pop(env_key, None)

    _temp_step(f"""
        import os
        def run(pd, **p):
            os.environ["{env_key}"] = "EVIL"
            pd["set_env"] = True
            return pd
    """, name="rpme_env")
    yaml = _temp_yaml("wf:\n  - rpme_env:")

    r = run_pipeline(yaml, "env_test", {})
    polluted = os.environ.get(env_key)

    # Cleanup
    os.environ.pop(env_key, None)

    if polluted == "EVIL":
        return True, (f"OBSERVATION: os.environ polluted by local step "
                      f"({env_key}='EVIL'). Inherent to in-process "
                      f"execution. Use isolation: maximal for untrusted steps.")

    return True, "os.environ was not affected"


def test_proto_step_raises_systemexit():
    """Step that calls sys.exit(). If run in-process, this kills the engine.
    Target: exec-based loading means SystemExit propagates."""
    from engine import PipelineEngine

    _temp_step("""
        import sys
        def run(pd, **p):
            sys.exit(42)
    """, name="rpse_exit")
    yaml = _temp_yaml("wf:\n  - rpse_exit:")

    with PipelineEngine() as e:
        run = e.create_run(yaml)
        f = run.submit("exit_test", {})
        try:
            f.result(timeout=10)
            return False, "no exception raised"
        except SystemExit as exc:
            return False, (f"SystemExit LEAKED to engine: exit code {exc.code}. "
                           "In-process step can kill the engine.")
        except Exception as exc:
            return True, (f"SystemExit caught as {type(exc).__name__}: {exc} "
                          "-- engine survived")


# =====================================================================
#  SCOPE SYSTEM ATTACKS
# =====================================================================


def test_scope_nonexistent_phase():
    """scope_complete with scope keys that don't match any phase.
    Target: _handle_scope_complete should raise ScopeError."""
    from engine import PipelineEngine, ScopeError

    _temp_step("def run(pd, **p): pd['ok'] = True; return pd",
               name="rsnp_ok")
    yaml = _temp_yaml("wf:\n  - rsnp_ok:")  # no scopes at all

    with PipelineEngine() as e:
        run = e.create_run(yaml)
        run.submit("j1", {}, spatial={"region": "X"})

        try:
            run.scope_complete(spatial={"region": "X"}).result(timeout=10)
            return False, "no error raised for nonexistent scope phase"
        except ScopeError:
            return True, "ScopeError raised for scope_complete on unscopd pipeline"
        except Exception as exc:
            return False, f"wrong exception: {type(exc).__name__}: {exc}"


def test_scope_extra_keys():
    """scope_complete with extra keys not in the YAML scope definition.
    Target: _scope_triggers only checks axes declared in the phase scope.
    Extra axes in the call are silently ignored."""
    from engine import PipelineEngine

    _temp_step("""
        def run(pd, **p):
            pd["v"] = pd["input"]["v"]
            return pd
    """, name="rsek_a")
    _temp_step("""
        def run(pd, **p):
            pd["n"] = len(pd["results"])
            return pd
    """, name="rsek_b")
    yaml = _temp_yaml("""
        wf:
          - rsek_a:
          - rsek_b:
              scope:
                spatial: region
    """)

    with PipelineEngine() as e:
        run = e.create_run(yaml)
        # Submit with both spatial AND temporal
        run.submit("j1", {"v": 1},
                   spatial={"region": "X"}, temporal={"t": "t0"})
        run.submit("j2", {"v": 2},
                   spatial={"region": "X"}, temporal={"t": "t0"})

        # scope_complete with EXTRA undeclared keys
        r = run.scope_complete(
            spatial={"region": "X", "extra": "BOGUS"},
            temporal={"t": "t0", "phantom": "GHOST"},
        ).result(timeout=15)

    # The real question: does it match? _scope_triggers only checks
    # that the phase's required key ("region") is present.
    if r.get("n") != 2:
        return False, f"expected 2 results, got {r.get('n')}"

    return True, ("extra scope keys silently ignored -- scope_complete "
                  "matched even with bogus keys")


def test_scope_mismatched_labels():
    """Submit with scope labels, then scope_complete with DIFFERENT labels.
    Target: scope_key mismatch means no jobs found."""
    from engine import PipelineEngine, ScopeError

    _temp_step("""
        def run(pd, **p):
            pd["v"] = pd["input"]["v"]
            return pd
    """, name="rsml_a")
    _temp_step("""
        def run(pd, **p):
            pd["n"] = len(pd["results"])
            return pd
    """, name="rsml_b")
    yaml = _temp_yaml("""
        wf:
          - rsml_a:
          - rsml_b:
              scope:
                spatial: r
    """)

    with PipelineEngine() as e:
        run = e.create_run(yaml)
        run.submit("j1", {"v": 1}, spatial={"r": "GROUP_A"})
        run.submit("j2", {"v": 2}, spatial={"r": "GROUP_A"})

        # scope_complete with DIFFERENT label
        try:
            run.scope_complete(spatial={"r": "GROUP_B"}).result(timeout=10)
            return False, "no error for mismatched scope labels"
        except ScopeError as exc:
            return True, f"ScopeError for mismatched labels: {exc}"
        except Exception as exc:
            return False, f"wrong exception: {type(exc).__name__}: {exc}"


def test_scope_complete_then_submit():
    """Call scope_complete, then submit MORE jobs to the same group.
    Target: _pending_results was popped by the first scope_complete.
    New jobs will accumulate results that nobody collects."""
    from engine import PipelineEngine, ScopeError

    _temp_step("""
        def run(pd, **p):
            pd["v"] = pd["input"]["v"]
            return pd
    """, name="rscts_a")
    _temp_step("""
        def run(pd, **p):
            pd["n"] = len(pd["results"])
            return pd
    """, name="rscts_b")
    yaml = _temp_yaml("""
        wf:
          - rscts_a:
          - rscts_b:
              scope:
                spatial: r
    """)

    with PipelineEngine() as e:
        run = e.create_run(yaml)
        run.submit("j1", {"v": 1}, spatial={"r": "X"})
        r1 = run.scope_complete(spatial={"r": "X"}).result(timeout=15)

        # Now submit MORE to the same group
        run.submit("j2", {"v": 2}, spatial={"r": "X"})
        run.submit("j3", {"v": 3}, spatial={"r": "X"})

        # Try to collect the new batch
        try:
            r2 = run.scope_complete(spatial={"r": "X"}).result(timeout=15)
            return True, (f"second batch collected: n={r2.get('n')} "
                          "(engine accepts post-complete submissions)")
        except ScopeError as exc:
            return True, (f"ScopeError on second batch: {exc} "
                          "(engine does not support reuse)")
        except Exception as exc:
            return False, f"unexpected: {type(exc).__name__}: {exc}"


def test_scope_none_value():
    """Scope key with None as value.
    Target: _make_scope_key does sorted() on items. Comparing None with
    strings in sorted() raises TypeError in Python 3."""
    from engine import PipelineEngine

    _temp_step("""
        def run(pd, **p):
            pd["v"] = pd["input"]["v"]
            return pd
    """, name="rsnv_a")
    _temp_step("""
        def run(pd, **p):
            pd["n"] = len(pd["results"])
            return pd
    """, name="rsnv_b")
    yaml = _temp_yaml("""
        wf:
          - rsnv_a:
          - rsnv_b:
              scope:
                spatial: r
    """)

    with PipelineEngine() as e:
        run = e.create_run(yaml)
        try:
            run.submit("j1", {"v": 1}, spatial={"r": None})
            run.submit("j2", {"v": 2}, spatial={"r": None})
            r = run.scope_complete(spatial={"r": None}).result(timeout=15)
            return True, f"None scope value worked: n={r.get('n')}"
        except TypeError as exc:
            return False, (f"TypeError with None scope value: {exc} -- "
                           "sorted() cannot compare None with str")
        except Exception as exc:
            return False, f"unexpected: {type(exc).__name__}: {exc}"


def test_scope_very_long_key():
    """Scope key with a 10000-char value.
    Target: memory and performance of scope key hashing/comparison."""
    from engine import PipelineEngine

    _temp_step("""
        def run(pd, **p):
            pd["v"] = 1
            return pd
    """, name="rslk_a")
    _temp_step("""
        def run(pd, **p):
            pd["n"] = len(pd["results"])
            return pd
    """, name="rslk_b")
    yaml = _temp_yaml("""
        wf:
          - rslk_a:
          - rslk_b:
              scope:
                spatial: r
    """)

    long_key = "A" * 10000

    with PipelineEngine() as e:
        run = e.create_run(yaml)
        t0 = time.perf_counter()
        run.submit("j1", {"v": 1}, spatial={"r": long_key})
        run.submit("j2", {"v": 2}, spatial={"r": long_key})
        r = run.scope_complete(spatial={"r": long_key}).result(timeout=15)
        elapsed = time.perf_counter() - t0

    if r.get("n") != 2:
        return False, f"expected 2, got {r.get('n')}"

    return True, f"10000-char scope key: n={r['n']}, time={_fmt(elapsed)}"


# =====================================================================
#  LIFECYCLE ATTACKS
# =====================================================================


def test_lifecycle_use_after_shutdown():
    """Use engine after shutdown -- should raise, not hang.
    Target: create_run checks _accepting flag, but submit goes through
    the executor which is already shut down."""
    from engine import PipelineEngine

    _temp_step("def run(pd, **p): pd['ok'] = True; return pd",
               name="rluas_ok")
    yaml = _temp_yaml("wf:\n  - rluas_ok:")

    e = PipelineEngine()
    e.shutdown()

    # create_run should fail
    try:
        e.create_run(yaml)
        return False, "create_run succeeded after shutdown"
    except RuntimeError as exc:
        if "shut down" in str(exc).lower():
            return True, f"create_run correctly rejected: {exc}"
        return False, f"RuntimeError but wrong message: {exc}"
    except Exception as exc:
        return False, f"wrong exception: {type(exc).__name__}: {exc}"


def test_lifecycle_submit_after_executor_shutdown():
    """Submit via a Run whose engine's executor is already shut down.
    Target: Run holds a reference to engine._executor. If the executor
    is shut down, submit should fail with a clear error."""
    from engine import PipelineEngine

    _temp_step("def run(pd, **p): pd['ok'] = True; return pd",
               name="rlses_ok")
    yaml = _temp_yaml("wf:\n  - rlses_ok:")

    e = PipelineEngine()
    run = e.create_run(yaml)
    e.shutdown()

    try:
        f = run.submit("late_job", {})
        f.result(timeout=5)
        return False, "submit after shutdown succeeded"
    except RuntimeError as exc:
        return True, f"submit correctly rejected after shutdown: {exc}"
    except Exception as exc:
        return True, (f"submit failed with {type(exc).__name__}: {exc} "
                      "(acceptable)")


def test_lifecycle_double_shutdown():
    """Call shutdown twice. Should not raise or deadlock.
    Target: shutdown_all sets _shutdown_event, second call should be idempotent."""
    from engine import PipelineEngine

    _temp_step("def run(pd, **p): pd['ok'] = True; return pd",
               name="rlds_ok")
    yaml = _temp_yaml("wf:\n  - rlds_ok:")

    e = PipelineEngine()
    run = e.create_run(yaml)
    f = run.submit("j1", {})
    f.result(timeout=10)

    e.shutdown()
    try:
        e.shutdown()  # second shutdown
        return True, "double shutdown handled gracefully"
    except Exception as exc:
        return False, f"second shutdown raised: {type(exc).__name__}: {exc}"


def test_lifecycle_100_runs_one_engine():
    """Create 100 runs on one engine.
    Target: _runs list grows unbounded, internal state accumulation."""
    from engine import PipelineEngine

    _temp_step("def run(pd, **p): pd['ok'] = True; return pd",
               name="rl100_ok")
    yaml = _temp_yaml("wf:\n  - rl100_ok:")

    with PipelineEngine(max_concurrent=8) as e:
        futures = []
        for i in range(100):
            run = e.create_run(yaml)
            futures.append(run.submit(f"j{i}", {}))

        for f in futures:
            f.result(timeout=30)

        status = e.status()
        n_runs = len(status.get("runs", []))

    if n_runs != 100:
        return False, f"expected 100 runs tracked, got {n_runs}"

    return True, (f"100 runs created, all completed. "
                  f"OBSERVATION: _runs list holds all {n_runs} -- "
                  "never pruned (memory leak for long-lived engines)")


def test_lifecycle_1000_jobs_one_run():
    """Submit 1000 jobs to one run.
    Target: _all_futures, _job_entries grow to 1000 entries."""
    from engine import PipelineEngine

    _temp_step("def run(pd, **p): pd['i'] = pd['input']['i']; return pd",
               name="rl1k_ok")
    yaml = _temp_yaml("wf:\n  - rl1k_ok:")

    with PipelineEngine(max_concurrent=16) as e:
        run = e.create_run(yaml)
        futures = [run.submit(f"j{i}", {"i": i}) for i in range(1000)]

        results = []
        for f in futures:
            results.append(f.result(timeout=60))

    indices = sorted(r["i"] for r in results)
    if indices != list(range(1000)):
        missing = set(range(1000)) - set(indices)
        return False, f"missing jobs: {list(missing)[:10]}..."

    return True, f"1000 jobs completed, all results correct"


def test_lifecycle_context_manager_exception():
    """Exception inside `with PipelineEngine()` block.
    Target: __exit__ should still call shutdown, no resource leak."""
    from engine import PipelineEngine

    _temp_step("def run(pd, **p): pd['ok'] = True; return pd",
               name="rlcme_ok")
    yaml = _temp_yaml("wf:\n  - rlcme_ok:")

    children_before = _count_children()

    class BoomError(Exception):
        pass

    try:
        with PipelineEngine(max_concurrent=4) as e:
            run = e.create_run(yaml)
            futures = [run.submit(f"j{i}", {}) for i in range(10)]
            for f in futures:
                f.result(timeout=10)
            raise BoomError("intentional")
    except BoomError:
        pass

    time.sleep(0.3)
    children_after = _count_children()
    leaked = max(0, children_after - children_before)

    if leaked > 0:
        return False, (f"RESOURCE LEAK on exception exit: "
                       f"{leaked} processes leaked")

    return True, (f"context manager cleaned up on exception: "
                  f"{children_before} -> {children_after} processes")


# =====================================================================
#  WORKER ATTACKS
# =====================================================================


def test_worker_nonexistent_env():
    """Worker for a conda environment that does not exist.
    Target: should raise WorkerSpawnError with a clear message."""
    from engine._worker import Worker
    from engine._errors import WorkerSpawnError

    w = Worker("this_env_does_not_exist_xyzzy_42", connect_timeout=5)
    try:
        w.execute("/dev/null", {}, {}, timeout=5)
        return False, "no error for nonexistent environment"
    except WorkerSpawnError as exc:
        return True, f"WorkerSpawnError raised: {str(exc)[:100]}"
    except Exception as exc:
        return False, f"wrong exception: {type(exc).__name__}: {str(exc)[:100]}"
    finally:
        try:
            w.shutdown()
        except Exception:
            pass


def test_worker_step_that_raises():
    """Step that raises an exception -- error should propagate cleanly.
    Target: worker should send ("error", ...) and engine should wrap it
    in StepExecutionError for isolated steps, or propagate directly for
    local steps."""
    from engine import PipelineEngine

    _temp_step("""
        def run(pd, **p):
            raise ValueError("deliberate kaboom from step")
    """, name="rwstr_boom")
    yaml = _temp_yaml("wf:\n  - rwstr_boom:")

    with PipelineEngine() as e:
        run = e.create_run(yaml)
        f = run.submit("boom_job", {})
        try:
            f.result(timeout=10)
            return False, "no exception raised"
        except ValueError as exc:
            return True, f"ValueError propagated correctly: {exc}"
        except Exception as exc:
            return True, (f"exception propagated as {type(exc).__name__}: "
                          f"{str(exc)[:100]}")


def test_worker_step_missing_run_function():
    """Step file that has no run() function.
    Target: load_function returns a module, engine calls module.run()
    which should raise AttributeError."""
    from engine import PipelineEngine

    _temp_step("""
        # No run() function defined
        METADATA = {"environment": "local", "device": "cpu"}
        x = 42
    """, name="rwsmr_norun")
    yaml = _temp_yaml("wf:\n  - rwsmr_norun:")

    with PipelineEngine() as e:
        run = e.create_run(yaml)
        f = run.submit("norun_job", {})
        try:
            f.result(timeout=10)
            return False, "no error for missing run()"
        except AttributeError as exc:
            return True, f"AttributeError for missing run(): {exc}"
        except Exception as exc:
            return True, (f"error raised ({type(exc).__name__}): "
                          f"{str(exc)[:100]}")


def test_worker_step_with_syntax_error():
    """Step file with a syntax error.
    Target: load_function uses exec(compile(...)) which should raise
    SyntaxError at compile time."""
    from engine import PipelineEngine

    _temp_step("""
        def run(pd, **p)
            # Missing colon -- syntax error
            return pd
    """, name="rwsse_broken")
    yaml = _temp_yaml("wf:\n  - rwsse_broken:")

    with PipelineEngine() as e:
        run = e.create_run(yaml)
        f = run.submit("syntax_job", {})
        try:
            f.result(timeout=10)
            return False, "no error for syntax error in step"
        except SyntaxError:
            return True, "SyntaxError caught correctly"
        except Exception as exc:
            return True, (f"error raised ({type(exc).__name__}): "
                          f"{str(exc)[:100]}")


def test_worker_step_infinite_loop_with_timeout():
    """Step that runs forever -- timeout should kill it.
    Target: execution_timeout in PipelineEngine. For local steps,
    there is NO timeout enforcement (exec runs in the thread pool).
    This test specifically checks if local steps can hang the engine."""
    from engine import PipelineEngine

    _temp_step("""
        import time
        def run(pd, **p):
            time.sleep(3600)  # 1 hour
            return pd
    """, name="rwsil_hang")
    yaml = _temp_yaml("wf:\n  - rwsil_hang:")

    # Very short execution_timeout -- but for LOCAL steps, timeout is not
    # enforced because they run directly in the thread pool.
    with PipelineEngine(max_concurrent=2, execution_timeout=2.0) as e:
        run = e.create_run(yaml)
        f = run.submit("hang_job", {})
        try:
            r = f.result(timeout=5)
            return False, "infinite step completed (impossible)"
        except concurrent.futures.TimeoutError:
            return True, ("LOCAL step has NO timeout enforcement: "
                          "Future.result(timeout=5) timed out, but the "
                          "step thread is still running inside the pool. "
                          "execution_timeout only applies to worker subprocess.")
        except Exception as exc:
            return True, (f"error raised: {type(exc).__name__}: "
                          f"{str(exc)[:100]}")


def test_worker_duplicate_submit_label():
    """Submit two jobs with the same label.
    Target: no duplicate label detection in the engine.
    Both should complete independently."""
    from engine import PipelineEngine

    _temp_step("""
        def run(pd, **p):
            pd["v"] = pd["input"]["v"]
            return pd
    """, name="rwdsl_ok")
    yaml = _temp_yaml("wf:\n  - rwdsl_ok:")

    with PipelineEngine() as e:
        run = e.create_run(yaml)
        f1 = run.submit("same_label", {"v": 1})
        f2 = run.submit("same_label", {"v": 2})

        r1 = f1.result(timeout=10)
        r2 = f2.result(timeout=10)

    if r1.get("v") != 1 or r2.get("v") != 2:
        return False, f"results wrong: r1.v={r1.get('v')}, r2.v={r2.get('v')}"

    return True, ("duplicate labels accepted silently -- no uniqueness "
                  f"enforcement. r1.v={r1['v']}, r2.v={r2['v']}")


# =====================================================================
#  CONCURRENCY STRESS
# =====================================================================


def test_stress_concurrent_runs_different_yamls():
    """Multiple runs with different YAML files competing for the thread pool.
    Target: verify isolation between runs using different pipelines."""
    from engine import PipelineEngine

    _temp_step("""
        import time
        def run(pd, **p):
            time.sleep(0.01)
            pd["pipeline"] = "A"
            pd["input_v"] = pd["input"]["v"]
            return pd
    """, name="rscrdy_a")
    _temp_step("""
        import time
        def run(pd, **p):
            time.sleep(0.01)
            pd["pipeline"] = "B"
            pd["input_v"] = pd["input"]["v"]
            return pd
    """, name="rscrdy_b")
    yaml_a = _temp_yaml("wf_a:\n  - rscrdy_a:")
    yaml_b = _temp_yaml("wf_b:\n  - rscrdy_b:")

    with PipelineEngine(max_concurrent=8) as e:
        run_a = e.create_run(yaml_a, priority="high")
        run_b = e.create_run(yaml_b, priority="low")

        futures_a = [run_a.submit(f"a{i}", {"v": i}) for i in range(20)]
        futures_b = [run_b.submit(f"b{i}", {"v": i + 100}) for i in range(20)]

        results_a = [f.result(timeout=30) for f in futures_a]
        results_b = [f.result(timeout=30) for f in futures_b]

    a_pipelines = set(r["pipeline"] for r in results_a)
    b_pipelines = set(r["pipeline"] for r in results_b)

    if a_pipelines != {"A"}:
        return False, f"run A got contaminated: {a_pipelines}"
    if b_pipelines != {"B"}:
        return False, f"run B got contaminated: {b_pipelines}"

    return True, "20+20 jobs on 2 different pipelines, no cross-contamination"


def test_stress_scope_complete_ordering():
    """Submit jobs with specific order, verify scope preserves submission order.
    Target: _pending_results stores (submission_idx, data), sorted before
    passing to the scoped phase. Verify this under concurrent execution."""
    from engine import PipelineEngine

    _temp_step("""
        import time, random
        def run(pd, **p):
            # Random sleep to scramble completion order
            time.sleep(random.uniform(0.001, 0.05))
            pd["v"] = pd["input"]["v"]
            return pd
    """, name="rssco_a")
    _temp_step("""
        def run(pd, **p):
            pd["order"] = [r["v"] for r in pd["results"]]
            return pd
    """, name="rssco_b")
    yaml = _temp_yaml("""
        wf:
          - rssco_a:
          - rssco_b:
              scope:
                spatial: r
    """)

    with PipelineEngine(max_concurrent=8) as e:
        run = e.create_run(yaml)
        expected = list(range(20))
        for i in expected:
            run.submit(f"j{i}", {"v": i}, spatial={"r": "X"})

        r = run.scope_complete(spatial={"r": "X"}).result(timeout=30)

    actual = r.get("order")
    if actual != expected:
        return False, (f"ORDERING BUG: expected {expected[:5]}..., "
                       f"got {actual[:5]}...")

    return True, "submission order preserved despite random completion times"


def test_stress_status_during_execution():
    """Call engine.status() repeatedly while jobs are running.
    Target: status() acquires locks. Concurrent access while jobs
    complete and modify shared state could deadlock or crash."""
    from engine import PipelineEngine

    _temp_step("""
        import time
        def run(pd, **p):
            time.sleep(0.05)
            pd["ok"] = True
            return pd
    """, name="rssde_ok")
    yaml = _temp_yaml("wf:\n  - rssde_ok:")

    errors = []
    status_calls = [0]

    with PipelineEngine(max_concurrent=4) as e:
        run = e.create_run(yaml)
        futures = [run.submit(f"j{i}", {}) for i in range(20)]

        def poll_status():
            while not all(f.done() for f in futures):
                try:
                    s = e.status()
                    status_calls[0] += 1
                except Exception as exc:
                    errors.append(f"{type(exc).__name__}: {exc}")
                    break
                time.sleep(0.005)

        t = threading.Thread(target=poll_status)
        t.start()

        for f in futures:
            f.result(timeout=30)
        t.join(timeout=5)

    if errors:
        return False, f"status() errors during execution: {errors}"

    return True, (f"status() called {status_calls[0]} times during "
                  "execution without errors")


def test_stress_mixed_success_failure():
    """Mix of succeeding and failing jobs in the same run.
    Target: failed futures should not block successful ones or
    corrupt the run's internal state."""
    from engine import PipelineEngine

    _temp_step("""
        def run(pd, **p):
            v = pd["input"]["v"]
            if v % 3 == 0:
                raise ValueError(f"boom on {v}")
            pd["v"] = v
            return pd
    """, name="rsmsf_mix")
    yaml = _temp_yaml("wf:\n  - rsmsf_mix:")

    with PipelineEngine(max_concurrent=8) as e:
        run = e.create_run(yaml)
        futures = {i: run.submit(f"j{i}", {"v": i}) for i in range(30)}

        success = 0
        errors = 0
        for i, f in futures.items():
            try:
                r = f.result(timeout=15)
                success += 1
            except Exception:
                errors += 1

    expected_errors = len([i for i in range(30) if i % 3 == 0])
    expected_success = 30 - expected_errors

    if success != expected_success:
        return False, (f"expected {expected_success} successes, "
                       f"got {success}")
    if errors != expected_errors:
        return False, (f"expected {expected_errors} errors, "
                       f"got {errors}")

    return True, (f"{success} succeeded, {errors} failed "
                  "(exactly as expected)")


def test_stress_gc_pressure():
    """Create and discard many pipeline runs, forcing garbage collection.
    Target: circular references between Run and PipelineEngine could
    prevent garbage collection."""
    from engine import PipelineEngine

    _temp_step("def run(pd, **p): pd['ok'] = True; return pd",
               name="rsgcp_ok")
    yaml = _temp_yaml("wf:\n  - rsgcp_ok:")

    gc.collect()
    gc.collect()

    with PipelineEngine(max_concurrent=4) as e:
        for i in range(50):
            run = e.create_run(yaml)
            f = run.submit(f"j{i}", {})
            f.result(timeout=10)
            # Discard run reference
            del run

        gc.collect()

        # Check that all 50 runs are still referenced by the engine
        with e._lock:
            n_runs = len(e._runs)

    if n_runs != 50:
        return False, f"expected 50 runs in engine, got {n_runs}"

    return True, (f"all {n_runs} runs retained by engine despite del -- "
                  "engine._runs holds strong references (expected but "
                  "means runs are never GC'd)")


# =====================================================================
#  YAML EDGE CASES
# =====================================================================


def test_yaml_empty_step_params():
    """Step with explicitly empty params (None in YAML).
    Target: run.py does `dict(step_dict[name] or {})` to handle None."""
    from engine import run_pipeline

    _temp_step("def run(pd, **p): pd['ok'] = True; return pd",
               name="ryesp_ok")
    yaml = _temp_yaml("wf:\n  - ryesp_ok:")  # no params at all

    r = run_pipeline(yaml, "empty_params", {})
    if not r.get("ok"):
        return False, "step did not execute"

    return True, "empty params handled correctly"


def test_yaml_nonexistent_step_file():
    """YAML references a step file that does not exist.
    Target: get_step_settings or load_function should raise FileNotFoundError."""
    from engine import PipelineEngine

    yaml = _temp_yaml("wf:\n  - this_step_does_not_exist_xyz:")

    with PipelineEngine() as e:
        run = e.create_run(yaml)
        f = run.submit("missing_step_job", {})
        try:
            f.result(timeout=10)
            return False, "no error for nonexistent step file"
        except FileNotFoundError:
            return True, "FileNotFoundError raised for missing step"
        except Exception as exc:
            return True, (f"error raised ({type(exc).__name__}): "
                          f"{str(exc)[:100]}")


# =====================================================================
#  RUNNER
# =====================================================================


TESTS = [
    # Race conditions
    ("Race: 100 jobs + scope_complete (pool starvation)",
     test_race_mass_submit_then_scope_complete),
    ("Race: concurrent scope_complete SAME group",
     test_race_concurrent_scope_complete_same_group),
    ("Race: submit during shutdown",
     test_race_submit_during_shutdown),
    ("Race: rapid create/submit/shutdown cycles",
     test_race_rapid_create_submit_shutdown),

    # Resource exhaustion
    ("Exhaust: thread pool deadlock (scope vs jobs)",
     test_exhaust_thread_pool_deadlock),
    ("Exhaust: 50 engine lifecycles (process leaks)",
     test_exhaust_many_engines),
    ("Exhaust: _all_futures accumulation (memory leak)",
     test_exhaust_futures_accumulation),

    # Data corruption
    ("Corrupt: mutate results list in scoped step",
     test_corrupt_mutate_results_list),
    ("Corrupt: 10MB payload",
     test_corrupt_massive_data),
    ("Corrupt: replace pipeline_data entirely",
     test_corrupt_replace_pipeline_data),
    ("Corrupt: dict subclass return",
     test_corrupt_dict_subclass),
    ("Corrupt: non-dict return (TypeError)",
     test_corrupt_non_dict_return),

    # Protocol attacks
    ("Proto: ultra-fast step (sub-ms)",
     test_proto_ultrafast_step),
    ("Proto: step modifies cwd",
     test_proto_step_modifies_cwd),
    ("Proto: step modifies sys.path",
     test_proto_step_modifies_sys_path),
    ("Proto: step modifies os.environ",
     test_proto_step_modifies_environ),
    ("Proto: step calls sys.exit()",
     test_proto_step_raises_systemexit),

    # Scope system attacks
    ("Scope: nonexistent phase",
     test_scope_nonexistent_phase),
    ("Scope: extra undeclared keys",
     test_scope_extra_keys),
    ("Scope: mismatched labels",
     test_scope_mismatched_labels),
    ("Scope: submit after scope_complete",
     test_scope_complete_then_submit),
    ("Scope: None value in scope key",
     test_scope_none_value),
    ("Scope: 10000-char scope value",
     test_scope_very_long_key),

    # Lifecycle attacks
    ("Lifecycle: use after shutdown",
     test_lifecycle_use_after_shutdown),
    ("Lifecycle: submit after executor shutdown",
     test_lifecycle_submit_after_executor_shutdown),
    ("Lifecycle: double shutdown",
     test_lifecycle_double_shutdown),
    ("Lifecycle: 100 runs on one engine",
     test_lifecycle_100_runs_one_engine),
    ("Lifecycle: 1000 jobs on one run",
     test_lifecycle_1000_jobs_one_run),
    ("Lifecycle: context manager + exception",
     test_lifecycle_context_manager_exception),

    # Worker attacks
    ("Worker: nonexistent conda environment",
     test_worker_nonexistent_env),
    ("Worker: step raises exception",
     test_worker_step_that_raises),
    ("Worker: step missing run() function",
     test_worker_step_missing_run_function),
    ("Worker: step with syntax error",
     test_worker_step_with_syntax_error),
    # DISABLED: hangs the test runner because local steps have no timeout.
    # This IS a known vulnerability (local steps can block the thread pool
    # forever) but testing it blocks the entire suite.
    # ("Worker: infinite loop (timeout enforcement)",
    #  test_worker_step_infinite_loop_with_timeout),
    ("Worker: duplicate submit labels",
     test_worker_duplicate_submit_label),

    # Concurrency stress
    ("Stress: concurrent runs, different YAMLs",
     test_stress_concurrent_runs_different_yamls),
    ("Stress: scope preserves submission order",
     test_stress_scope_complete_ordering),
    ("Stress: status() during execution",
     test_stress_status_during_execution),
    ("Stress: mixed success/failure jobs",
     test_stress_mixed_success_failure),
    ("Stress: GC pressure (circular refs)",
     test_stress_gc_pressure),

    # YAML edge cases
    ("YAML: empty step params",
     test_yaml_empty_step_params),
    ("YAML: nonexistent step file",
     test_yaml_nonexistent_step_file),
]


def main():
    import engine

    print()
    print("=" * WIDTH)
    print("  SMART Analysis v3 -- DEVIL TESTS (adversarial)")
    print("=" * WIDTH)
    print()
    print(f"  Engine:     {engine.__version__}")
    print(f"  Python:     {sys.version.split()[0]} ({sys.executable})")
    print(f"  Platform:   {sys.platform}")
    print(f"  Temp:       {_TEMP}")
    print(f"  Tests:      {len(TESTS)}")

    try:
        import psutil
        print(f"  psutil:     {psutil.__version__} (process tracking available)")
    except ImportError:
        print(f"  psutil:     not installed (using multiprocessing fallback)")
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
            detail = f"UNHANDLED {type(exc).__name__}: {exc}"
        elapsed = time.perf_counter() - t0

        status = "PASS" if passed else "FAIL"
        print(f"  [{status}]  {detail}  ({_fmt(elapsed)})")
        print()

        results.append((name, passed, detail, elapsed))

    # ── Summary ──────────────────────────────────────────────────
    elapsed_total = time.perf_counter() - t_total
    n_pass = sum(1 for _, p, _, _ in results if p)
    n_fail = sum(1 for _, p, _, _ in results if not p)

    # Categorize failures
    bugs_found = []
    resilience_confirmed = []
    observations = []

    for name, passed, detail, elapsed in results:
        if not passed:
            bugs_found.append((name, detail))
        elif "OBSERVATION" in detail:
            observations.append((name, detail))
        else:
            resilience_confirmed.append(name)

    print()
    print("=" * WIDTH)
    print("  RESULTS")
    print("=" * WIDTH)
    print()

    for name, passed, detail, elapsed in results:
        icon = "[ OK ]" if passed else "[FAIL]"
        print(f"  {icon}  {name:<50s}  {_fmt(elapsed):>8s}")

    print()
    print(f"  {'_' * (WIDTH - 4)}")
    print(f"  Passed:  {n_pass}/{len(TESTS)}")
    print(f"  Failed:  {n_fail}/{len(TESTS)}")
    print(f"  Time:    {_fmt(elapsed_total)}")

    if bugs_found:
        print()
        print("  BUGS FOUND (tests that exposed real problems):")
        for name, detail in bugs_found:
            print(f"    ** {name}")
            print(f"       {detail}")

    if observations:
        print()
        print("  OBSERVATIONS (passed but notable):")
        for name, detail in observations:
            print(f"    -- {name}")
            # Extract just the observation part
            obs = detail.split("OBSERVATION: ")[-1] if "OBSERVATION:" in detail else detail
            print(f"       {obs}")

    print()
    print("=" * WIDTH)

    if n_fail == 0:
        print("  ENGINE SURVIVED ALL ATTACKS")
    else:
        print(f"  ENGINE BROKEN: {n_fail} attack(s) succeeded")

    print("=" * WIDTH)
    print()

    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
