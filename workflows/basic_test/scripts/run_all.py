"""
Run all v3 engine tests: pytest suite + integration pipeline tests.

Automatically sets up test environments before running,
and cleans them up afterwards. Writes a detailed log file
alongside this script for sharing / debugging.

Usage:
    python run_all.py
    python run_all.py --keep-envs    # do not clean up after
    python run_all.py --skip-setup   # assume envs already exist

Test phases:
    1. Environment setup    (conda envs for isolation tests)
    2. Pytest suite         (108 tests: unit, integration, stress)
    3. Pipeline YAML tests  (9 pipelines via run_pipeline API)
    4. Cleanup              (remove conda envs)
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
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "pyyaml", "-q"])

WORKFLOW_DIR = Path(__file__).parent.parent
ROOT = WORKFLOW_DIR.parent.parent
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
    try:
        from engine.conda_utils import get_conda_info, get_conda_exe
        return get_conda_exe(get_conda_info())
    except Exception:
        return "conda"


TEST_ENV = "SMART--basic_test--env_a"


def run_pytest():
    """Run the engine pytest suite, streaming filtered output to console.

    Runs from the current Python environment (which has pytest + pyyaml).
    Isolation tests spawn subprocesses into conda envs as needed.
    """
    test_file = ROOT / "engine" / "test_engine.py"

    cmd = [sys.executable, "-m", "pytest", str(test_file),
           "-v", "--tb=short"]

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, env=env,
    )

    lines = []
    current_class = ""

    for line in lines_iter(proc):
        lines.append(line)

        # Detect pytest verbose output lines. Formats:
        #   "path::Class::test PASSED"        (no -- prefix)
        #   "path::Class::test_name PASSED"   (with underscores)
        #   Windows/macOS path separators vary
        m = re.search(r"::(\w+)::(\w+)\s+(PASSED|FAILED|ERROR|SKIPPED)", line)
        if m:
            cls, test, status = m.group(1), m.group(2), m.group(3)
            if cls != current_class:
                current_class = cls
                print(f"\n    {cls}")
            icons = {"PASSED": "ok", "FAILED": "** FAIL **",
                     "ERROR": "** ERROR **", "SKIPPED": "skip"}
            print(f"      {test:<50s} {icons.get(status, status)}")

        # Show summary lines
        elif line.startswith("=") and ("passed" in line or "failed" in line
                                        or "error" in line):
            print(f"    {line.strip()}")

    proc.wait()
    output = "\n".join(lines)

    # Parse summary: "X passed, Y failed, Z skipped in Ns"
    passed = failed = skipped = total = 0
    m_summary = re.search(
        r"(\d+) passed", output)
    if m_summary:
        passed = int(m_summary.group(1))
    m_fail = re.search(r"(\d+) failed", output)
    if m_fail:
        failed = int(m_fail.group(1))
    m_skip = re.search(r"(\d+) skipped", output)
    if m_skip:
        skipped = int(m_skip.group(1))

    total = passed + failed + skipped
    ok = proc.returncode == 0

    return passed, failed, skipped, total, ok, output


def run_pipeline_tests(log, base):
    """Run integration pipeline tests via run_pipeline(). Returns results."""
    results = []

    for i, (name, yaml_file, should_pass, description) in enumerate(
            PIPELINE_TESTS, 1):
        yaml_path = base / yaml_file

        log(f"  [{i:2d}/{len(PIPELINE_TESTS)}] {name:<20s} {description}")

        passed = False
        error_msg = ""
        detail = ""

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
        description="Run all SMART Analysis v3 tests")
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
    base = WORKFLOW_DIR
    setup_script = base / "environments" / "setup_env.py"
    clean_script = base / "environments" / "clean_env.py"
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    t_start = time.time()

    log()
    log("=" * WIDTH)
    log("  SMART Analysis v3 -- Full Test Suite")
    log("=" * WIDTH)
    log()

    sysinfo = system_info()
    for label, value in sysinfo:
        log(f"  {label + ':':<20s} {value}")
    log()

    setup_output = ""
    cleanup_output = ""
    pt_passed = pt_failed = pt_skipped = pt_total = 0
    pt_ok = False
    pt_output = ""
    yaml_results = []

    try:
        # ──────────────────────────────────────────────────────
        # Phase 1: Environment Setup
        # ──────────────────────────────────────────────────────
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

        # ──────────────────────────────────────────────────────
        # Phase 2: Pytest Suite (unit + integration + stress)
        # ──────────────────────────────────────────────────────
        log("=" * WIDTH)
        log("  Phase 2: Pytest Suite")
        log("=" * WIDTH)
        log()

        pt_passed, pt_failed, pt_skipped, pt_total, pt_ok, pt_output = \
            run_pytest()

        if pt_ok:
            log(f"  [ OK ]    {pt_passed}/{pt_total} passed"
                + (f" ({pt_skipped} skipped)" if pt_skipped else ""))
        else:
            log(f"  [FAIL]    {pt_passed} passed, {pt_failed} failed"
                f" out of {pt_total}")
            log()
            for line in pt_output.splitlines():
                if "FAILED" in line:
                    log(f"            {line.strip()}")
        log()

        # ──────────────────────────────────────────────────────
        # Phase 3: Pipeline YAML Tests
        # ──────────────────────────────────────────────────────
        log("=" * WIDTH)
        log("  Phase 3: Pipeline YAML Tests")
        log("=" * WIDTH)
        log()

        yaml_results = run_pipeline_tests(log, base)

        # ──────────────────────────────────────────────────────
        # Phase 4: Cleanup
        # ──────────────────────────────────────────────────────
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

    # ──────────────────────────────────────────────────────────
    # Summary
    # ──────────────────────────────────────────────────────────
    elapsed = time.time() - t_start

    yaml_passed = sum(1 for _, ok, _ in yaml_results if ok)
    yaml_total = len(yaml_results)

    total_passed = pt_passed + yaml_passed
    total_tests = pt_total + yaml_total
    all_ok = pt_ok and (yaml_passed == yaml_total)

    log("=" * WIDTH)
    log("  Results")
    log("=" * WIDTH)

    log()
    log(f"  Pytest suite:          {pt_passed}/{pt_total}"
        + (f" ({pt_skipped} skipped)" if pt_skipped else "")
        + ("  ** FAILURES **" if not pt_ok else ""))

    log()
    for name, correct, _ in yaml_results:
        status = "[ OK ]" if correct else "[FAIL]"
        log(f"  {status}    {name}")

    log()
    log(f"  {'_' * (WIDTH - 4)}")
    log(f"  Pytest:                {pt_passed}/{pt_total}")
    log(f"  Pipeline YAML:         {yaml_passed}/{yaml_total}")
    log(f"  Total:                 {total_passed}/{total_tests}")
    log(f"  Time:                  {elapsed:.0f}s")
    log()

    if all_ok:
        log("  ALL TESTS PASSED")
    else:
        log("  SOME TESTS FAILED")

    log()
    log("=" * WIDTH)

    # ──────────────────────────────────────────────────────────
    # Detailed log file
    # ──────────────────────────────────────────────────────────
    log.detail("")
    log.detail("")
    log.detail("=" * WIDTH)
    log.detail("  DETAILED LOG")
    log.detail("=" * WIDTH)

    log.detail("")
    log.detail("--- system ---")
    for label, value in sysinfo:
        log.detail(f"  {label}: {value}")
    log.detail("--- end system ---")

    if setup_output:
        log.detail("")
        log.detail("--- environment setup ---")
        log.detail(setup_output.rstrip())
        log.detail("--- end environment setup ---")

    if pt_output:
        log.detail("")
        log.detail("--- pytest output ---")
        log.detail(pt_output.rstrip())
        log.detail("--- end pytest output ---")

    for name, correct, detail in yaml_results:
        if detail.strip():
            status = "OK" if correct else "FAIL"
            log.detail("")
            log.detail(f"--- pipeline: {name} [{status}] ---")
            log.detail(detail.rstrip())
            log.detail(f"--- end pipeline: {name} ---")

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
