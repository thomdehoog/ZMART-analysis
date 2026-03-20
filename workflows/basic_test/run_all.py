"""
Run all tests: engine unit tests + integration pipeline tests.

Automatically sets up test environments before running,
and cleans them up afterwards. Writes a detailed log file
alongside this script for sharing / debugging.

Usage:
    python run_all.py
    python run_all.py --keep-envs    # do not clean up after
    python run_all.py --skip-setup   # assume envs already exist
"""

import io
import os
import platform
import re
import sys
import subprocess
import time
import traceback
import argparse
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pyyaml", "-q"])

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))
from engine import run_pipeline

WIDTH = 70

PIPELINE_TESTS = [
    # (name, yaml, should_pass, description)
    ("local",
     "pipelines/test_local_pipeline.yaml",
     True,
     "Single local step"),

    ("mixed",
     "pipelines/test_mixed_pipeline.yaml",
     True,
     "Data flow between local steps"),

    ("step_env",
     "pipelines/test_step_env_pipeline.yaml",
     True,
     "Step level environment switching"),

    ("pipeline_env",
     "pipelines/test_pipeline_env_pipeline.yaml",
     True,
     "Pipeline level environment switching"),

    ("combined",
     "pipelines/test_combined_env_pipeline.yaml",
     True,
     "Nested environment switching"),

    ("data_survival",
     "pipelines/test_data_survival_pipeline.yaml",
     True,
     "Data survives env switch serialization"),

    ("pickle",
     "pipelines/test_pickle_pipeline.yaml",
     True,
     "Pickle data transfer between envs"),

    ("error",
     "pipelines/test_error_pipeline.yaml",
     False,
     "Engine handles step errors gracefully"),

    ("missing_step",
     "pipelines/test_missing_step_pipeline.yaml",
     False,
     "Engine handles missing step files"),
]


# ── Logging ───────────────────────────────────────────────────


class Log:
    """Collects log lines. Prints to console and writes to file."""

    def __init__(self):
        self._lines = []

    def __call__(self, text=""):
        """Print to console and record for log file."""
        print(text)
        self._lines.append(text)

    def detail(self, text=""):
        """Record for log file only (not printed to console)."""
        self._lines.append(text)

    def write(self, path):
        """Write all collected lines to a file."""
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(self._lines) + "\n")


# ── Helpers ───────────────────────────────────────────────────


def system_info():
    """Collect system information as a list of (label, value) pairs."""
    info = [
        ("Platform", f"{platform.system()} {platform.release()} "
                     f"({platform.machine()})"),
        ("Python", f"{sys.version.split()[0]} ({sys.executable})"),
        ("Working dir", str(ROOT)),
    ]

    # Conda
    conda_exe = os.environ.get("CONDA_EXE", "")
    if conda_exe:
        try:
            r = subprocess.run(
                [conda_exe, "--version"],
                capture_output=True, text=True, timeout=10,
            )
            info.append(("Conda", r.stdout.strip()))
        except Exception:
            info.append(("Conda", conda_exe))

    # Engine version
    try:
        import engine
        info.append(("Engine", engine.__version__))
    except Exception:
        pass

    return info


def lines_iter(proc):
    """Yield stripped lines from a subprocess stdout."""
    for line in proc.stdout:
        yield line.rstrip("\n\r")


def run_script(script_path):
    """Run a Python script, streaming output to console and capturing it."""
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [sys.executable, str(script_path)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, env=env,
    )
    lines = []
    for line in proc.stdout:
        print(line, end="")
        lines.append(line)
    proc.wait()
    return proc.returncode == 0, "".join(lines)


def _get_conda_exe():
    """Find the conda executable."""
    conda_exe = os.environ.get("CONDA_EXE", "")
    if conda_exe and Path(conda_exe).exists():
        return conda_exe
    # Fallback: try importing from engine
    try:
        from engine.conda_utils import get_conda_info, get_conda_exe
        return get_conda_exe(get_conda_info())
    except Exception:
        return "conda"


TEST_ENV = "SMART--basic_test--env_a"


def run_unit_tests():
    """Run engine unit tests via unittest in the test environment."""
    test_file = ROOT / "engine" / "test_engine.py"
    conda = _get_conda_exe()

    cmd = [conda, "run", "--no-capture-output", "-n", TEST_ENV,
           "python", "-m", "unittest", "discover",
           "-s", str(ROOT / "engine"), "-p", "test_engine.py",
           "-t", str(ROOT), "-v"]

    # Stream filtered output to console, capture everything for log
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, env=env,
    )
    lines = []
    current_class = ""
    pending_test = None  # (test_name, cls) waiting for result

    for line in lines_iter(proc):
        lines.append(line)

        # Detect test start: "test_name (engine.test_engine.Class)"
        m_start = re.match(
            r"(\w+) \(engine\.test_engine\.(\w+)\)", line)
        if m_start:
            pending_test = (m_start.group(1), m_start.group(2))

        # Detect result at end of line: "... ok" / "... FAIL" / "... ERROR"
        m_result = re.search(r"\.\.\. (ok|FAIL|ERROR|skipped\b.*)", line)
        if m_result and pending_test:
            test_name, cls = pending_test
            status = m_result.group(1)
            if cls != current_class:
                current_class = cls
                print(f"\n    {cls}")
            if status == "ok":
                icon = "ok"
            elif status.startswith("skipped"):
                icon = "skip"
            else:
                icon = f"** {status.upper()} **"
            print(f"      {test_name:<45s} {icon}")
            pending_test = None

        # Show the summary line
        elif line.startswith("Ran ") or line.startswith("OK") or \
                line.startswith("FAILED"):
            print(f"    {line}")

    proc.wait()
    output = "".join(l + "\n" for l in lines)

    # Parse summary: "Ran 95 tests in 57.83s" and "OK" / "FAILED (failures=N)"
    passed = failed = skipped = 0
    total = 0
    for line in output.splitlines():
        m_ran = re.search(r"Ran (\d+) test", line)
        if m_ran:
            total = int(m_ran.group(1))
        m_fail = re.search(r"FAILED \(.*?failures=(\d+)", line)
        if m_fail:
            failed += int(m_fail.group(1))
        m_err = re.search(r"FAILED \(.*?errors=(\d+)", line)
        if m_err:
            failed += int(m_err.group(1))
        m_skip = re.search(r"(?:OK|FAILED).*?skipped=(\d+)", line)
        if m_skip:
            skipped = int(m_skip.group(1))

    passed = total - failed - skipped
    ok = proc.returncode == 0

    return passed, failed, skipped, total, ok, output


def run_pipeline_tests(log, base):
    """Run integration pipeline tests. Returns list of (name, ok, detail)."""
    results = []

    for i, (name, yaml_file, should_pass, description) in enumerate(
            PIPELINE_TESTS, 1):
        yaml_path = base / yaml_file

        log(f"  [{i:2d}/{len(PIPELINE_TESTS)}] {name:<20s} {description}")

        passed = False
        error_msg = ""
        detail = ""

        # Capture engine output
        capture = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = capture

        try:
            run_pipeline(
                yaml_path=str(yaml_path),
                label=f"test_{name}",
                input_data={},
            )
            passed = True
        except Exception as e:
            error_msg = str(e)
            detail = traceback.format_exc()
            passed = False
        finally:
            sys.stdout = old_stdout
            detail = capture.getvalue() + detail

        if should_pass:
            if passed:
                log(f"         [ OK ]    passed as expected")
            else:
                log(f"         [FAIL]    expected pass, got error:")
                log(f"                   {error_msg[:80]}")
        else:
            if not passed:
                log(f"         [ OK ]    failed as expected "
                    f"({error_msg[:50]})")
            else:
                log(f"         [FAIL]    expected failure, but passed")

        correct = (passed == should_pass)
        results.append((name, correct, detail))
        log()

    return results


# ── Main ──────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Run all SMART Analysis tests")
    parser.add_argument(
        "--keep-envs", action="store_true",
        help="Do not clean up environments after tests",
    )
    parser.add_argument(
        "--skip-setup", action="store_true",
        help="Skip environment setup (assume envs exist)",
    )
    args = parser.parse_args()

    log = Log()
    base = Path(__file__).parent
    setup_script = base / "environments" / "setup_env.py"
    clean_script = base / "environments" / "clean_env.py"
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    t_start = time.time()

    log()
    log("=" * WIDTH)
    log("  SMART Analysis -- Full Test Suite")
    log("=" * WIDTH)
    log()

    # System info
    sysinfo = system_info()
    for label, value in sysinfo:
        log(f"  {label + ':':<20s} {value}")
    log()

    # State for results (populated by test phases)
    setup_output = ""
    cleanup_output = ""
    ut_passed = ut_failed = ut_skipped = ut_total = 0
    ut_ok = False
    ut_output = ""
    pt_results = []
    try:
        # --------------------------------------------------------------
        # Phase 1: Environment Setup
        # --------------------------------------------------------------
        if not args.skip_setup:
            log("=" * WIDTH)
            log("  Phase 1: Environment Setup")
            log("=" * WIDTH)
            log()

            setup_ok, setup_output = run_script(setup_script)
            if setup_ok:
                log("  [ OK ]    Environments ready")
            else:
                log("  [FAIL]    Environment setup failed. Cannot run tests.")
                raise SystemExit(1)

            log()
        else:
            log("  (environment setup skipped)")
            log()

        # --------------------------------------------------------------
        # Phase 2: Engine Unit Tests
        # --------------------------------------------------------------
        log("=" * WIDTH)
        log("  Phase 2: Engine Unit Tests")
        log("=" * WIDTH)
        log()

        ut_passed, ut_failed, ut_skipped, ut_total, ut_ok, ut_output = \
            run_unit_tests()

        if ut_ok:
            log(f"  [ OK ]    {ut_passed}/{ut_total} passed"
                + (f" ({ut_skipped} skipped)" if ut_skipped else ""))
        else:
            log(f"  [FAIL]    {ut_passed} passed, {ut_failed} failed"
                f" out of {ut_total}")
            log()
            for line in ut_output.splitlines():
                if "FAILED" in line:
                    log(f"            {line.strip()}")

        log()

        # --------------------------------------------------------------
        # Phase 3: Integration Pipeline Tests
        # --------------------------------------------------------------
        log("=" * WIDTH)
        log("  Phase 3: Integration Pipeline Tests")
        log("=" * WIDTH)
        log()

        pt_results = run_pipeline_tests(log, base)

        # --------------------------------------------------------------
        # Phase 4: Cleanup
        # --------------------------------------------------------------
        if not args.keep_envs and not args.skip_setup:
            log("=" * WIDTH)
            log("  Phase 4: Cleanup")
            log("=" * WIDTH)
            log()
            _, cleanup_output = run_script(clean_script)
            log("  Done")
            log()

    except SystemExit:
        pass

    # ------------------------------------------------------------------
    # Summary (always runs, even after interrupt)
    # ------------------------------------------------------------------
    elapsed = time.time() - t_start

    pt_passed = sum(1 for _, ok, _ in pt_results if ok)
    pt_total = len(pt_results)

    total_passed = ut_passed + pt_passed
    total_tests = ut_total + pt_total
    all_ok = ut_ok and (pt_passed == pt_total)

    log("=" * WIDTH)
    log("  Results")
    log("=" * WIDTH)

    log()
    log(f"  Engine unit tests:     {ut_passed}/{ut_total}"
        + (f" ({ut_skipped} skipped)" if ut_skipped else "")
        + ("  ** FAILURES **" if not ut_ok else ""))

    log()
    for name, correct, _ in pt_results:
        status = "[ OK ]" if correct else "[FAIL]"
        log(f"  {status}    {name}")

    log()
    log(f"  {'_' * (WIDTH - 4)}")
    log(f"  Unit tests:            {ut_passed}/{ut_total}")
    log(f"  Pipeline tests:        {pt_passed}/{pt_total}")
    log(f"  Total:                 {total_passed}/{total_tests}")
    log(f"  Time:                  {elapsed:.0f}s")
    log()

    if all_ok:
        log("  ALL TESTS PASSED")
    else:
        log("  SOME TESTS FAILED")

    log()
    log("=" * WIDTH)

    # ------------------------------------------------------------------
    # Write log file (always, even after interrupt or failure)
    # ------------------------------------------------------------------

    log.detail("")
    log.detail("")
    log.detail("=" * WIDTH)
    log.detail("  DETAILED LOG")
    log.detail("=" * WIDTH)

    # System info
    log.detail("")
    log.detail("--- system ---")
    for label, value in sysinfo:
        log.detail(f"  {label}: {value}")
    log.detail("--- end system ---")

    # Setup output
    if setup_output:
        log.detail("")
        log.detail("--- environment setup ---")
        log.detail(setup_output.rstrip())
        log.detail("--- end environment setup ---")

    # Full pytest output
    if ut_output:
        log.detail("")
        log.detail("--- pytest output ---")
        log.detail(ut_output.rstrip())
        log.detail("--- end pytest output ---")

    # Per-pipeline detail (engine output, tracebacks)
    for name, correct, detail in pt_results:
        if detail.strip():
            status = "OK" if correct else "FAIL"
            log.detail("")
            log.detail(f"--- pipeline: {name} [{status}] ---")
            log.detail(detail.rstrip())
            log.detail(f"--- end pipeline: {name} ---")

    # Cleanup output
    if cleanup_output:
        log.detail("")
        log.detail("--- cleanup ---")
        log.detail(cleanup_output.rstrip())
        log.detail("--- end cleanup ---")

    log_path = base / f"test_log_{timestamp}.txt"
    log.write(log_path)
    print(f"\n  Log:  {log_path}")

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
