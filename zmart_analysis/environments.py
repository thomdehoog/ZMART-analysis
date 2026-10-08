"""
Creating and removing a workflow's conda environments.

Every workflow keeps two short scripts, ``environments/setup_env.py`` and
``environments/clean_env.py``. They say which packages the workflow needs
and how to check them, and hand the rest to the two functions here:
``setup_workflow_env`` creates a ``ZMART--<workflow>--<step>`` environment,
installs the packages into it and runs the checks; ``clean_workflow_envs``
removes such environments again. Both print a plain progress report.

GPU detection lives here too, because it decides which PyTorch build a
setup installs.
"""

from __future__ import annotations

import argparse
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .conda import env_exists, get_conda_exe, get_conda_info, list_envs_by_prefix


def detect_gpu():
    """Auto-detect available GPU acceleration and CUDA version.

    Returns
    -------
    str
        "cu128", "cu124", etc. for NVIDIA CUDA (matched to installed version),
        "mps" for Apple Silicon, "cpu" otherwise.
    """
    system = platform.system()

    if system == "Darwin":
        if platform.machine() == "arm64":
            return "mps"
        return "cpu"

    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return "cpu"

    try:
        result = subprocess.run(
            [nvidia_smi],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return "cpu"

        for line in result.stdout.split("\n"):
            if "CUDA Version" in line:
                match = re.search(r"CUDA Version:\s*(\d+)\.(\d+)", line)
                if match:
                    major, minor = int(match.group(1)), int(match.group(2))
                    cuda_tag = f"cu{major}{minor}"
                    available = ["cu128", "cu126", "cu124", "cu121", "cu118"]
                    if cuda_tag in available:
                        return cuda_tag
                    for tag in available:
                        tag_major = int(tag[2:-1])
                        tag_minor = int(tag[-1])
                        if tag_major < major or (tag_major == major and tag_minor <= minor):
                            return tag
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # nvidia-smi is there but its driver version did not parse: the build
    # most drivers of the last years run.
    return "cu124"


def gpu_label(gpu):
    """Human-readable label for the GPU backend."""
    if gpu == "mps":
        return "Apple MPS (Metal Performance Shaders)"
    elif gpu == "cpu":
        return "CPU (no GPU acceleration)"
    else:
        version = f"{gpu[2:-1]}.{gpu[-1]}"
        return f"NVIDIA CUDA {version}"


def get_torch_install_args(gpu):
    """Get pip install arguments for PyTorch based on GPU type."""
    if gpu == "mps":
        return ["torch", "torchvision"]
    elif gpu == "cpu":
        return ["torch", "torchvision", "--index-url", "https://download.pytorch.org/whl/cpu"]
    else:
        return ["torch", "torchvision", "--index-url", f"https://download.pytorch.org/whl/{gpu}"]


# --------------------------------------------------------------------------
# Creating and removing a workflow's environments
# --------------------------------------------------------------------------

WIDTH = 70


def banner(title: str) -> None:
    print()
    print("=" * WIDTH)
    print(f"  {title}")
    print("=" * WIDTH)


def section(title: str) -> None:
    print()
    print(f"  {title}")
    print(f"  {'-' * (WIDTH - 4)}")


def info(label: str, value: str) -> None:
    print(f"  {label + ':':<24s} {value}")


def ok(message: str) -> None:
    print(f"  [ OK ]    {message}")


def fail(message: str) -> None:
    print(f"  [FAIL]    {message}")


def skip(message: str) -> None:
    print(f"  [SKIP]    {message}")


def warn(message: str) -> None:
    print(f"  [WARN]    {message}")


def cmd_line(cmd: list[str]) -> None:
    print(f"  [ RUN]    {' '.join(cmd)}")


def setup_workflow_env(
    *,
    workflow: str,
    pip_packages: list[str],
    diagnostics: list[tuple[str, str]],
    python_version: str = "3.12",
    install_torch: bool = True,
    default_step: str = "main",
    steps_dir: str | Path | None = None,
) -> None:
    """Create a ZMART--<workflow>--<step> conda environment and check it.

    *diagnostics* are ``(label, python one-liner)`` pairs run inside the new
    environment; each should print a short result. When *steps_dir* is given,
    ``__STEPS__`` in a one-liner is replaced with it, so a check can import
    the workflow's own steps. Exits the script with an error if any step of
    the setup fails.
    """
    if steps_dir is not None:
        diagnostics = [
            (label, code.replace("__STEPS__", str(steps_dir))) for label, code in diagnostics
        ]
    parser = argparse.ArgumentParser(description=f"Set up conda env for {workflow} workflow")
    parser.add_argument(
        "--step",
        default=default_step,
        help=f"Step name for isolation (default: {default_step})",
    )
    parser.add_argument(
        "--python",
        default=python_version,
        help=f"Python version (default: {python_version})",
    )
    parser.add_argument(
        "--gpu",
        default=None,
        help="PyTorch GPU backend: cu128, cu124, cu121, mps, cpu (default: auto-detect)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands without executing",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run the checks on an environment that already exists, installing nothing",
    )
    args = parser.parse_args()

    env_name = f"ZMART--{workflow}--{args.step}"
    gpu = (args.gpu or detect_gpu()) if install_torch else None
    t_start = time.time()

    banner("ZMART Analysis -- Environment Setup")

    section("System")
    info("Platform", f"{platform.system()} ({platform.machine()})")
    info("Python target", args.python)
    info("GPU backend", gpu_label(gpu) if install_torch else "not used")

    try:
        conda_info = get_conda_info()
    except FileNotFoundError as e:
        fail(str(e))
        sys.exit(1)

    conda_version = conda_info.get("conda_version", "unknown")
    _warn_old_conda(conda_version)
    info("Conda version", conda_version)

    section("Conda")
    conda = get_conda_exe(conda_info)
    envs_dirs = conda_info.get("envs_dirs", [])

    info("Executable", conda)
    info("Root prefix", conda_info.get("root_prefix", "unknown"))
    info("Envs directory", envs_dirs[0] if envs_dirs else "unknown")

    exists = env_exists(conda_info, env_name)
    if args.check:
        if not exists:
            fail(f"Environment '{env_name}' does not exist yet.")
            print(f"         Create it: python setup_env.py --step {args.step}")
            sys.exit(1)
        section(f"Checking {env_name}")
        _run_diagnostics(conda, env_name, diagnostics)
        banner("Check Passed")
        return
    if exists:
        fail(f"Environment '{env_name}' already exists.")
        print(f"         Check it:        python setup_env.py --step {args.step} --check")
        print(f"         Or remove it first: python clean_env.py --step {args.step}")
        sys.exit(1)

    section("Environment")
    info("Name", env_name)
    info("Workflow", workflow)
    info("Step", args.step)

    total_steps = 4 if install_torch else 3

    section(f"[1/{total_steps}] Creating conda environment")
    create_cmd = [
        conda,
        "create",
        "-n",
        env_name,
        f"python={args.python}",
        # conda-forge and nothing else: without a channel named here conda
        # falls back to `defaults`, whose terms of service the lab does not
        # accept, and a rig's base config may add it implicitly.
        "--override-channels",
        "-c",
        "conda-forge",
        "-y",
        "-q",
    ]
    cmd_line(create_cmd)
    if args.dry_run:
        skip("dry run")
    else:
        result = subprocess.run(create_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            fail("Failed to create environment")
            print(result.stderr)
            sys.exit(1)
        ok(f"Created {env_name}")

    next_step = 2
    if install_torch:
        section(f"[{next_step}/{total_steps}] Installing PyTorch ({gpu_label(gpu)})")
        pip_cmd = [
            conda,
            "run",
            "--no-capture-output",
            "-n",
            env_name,
            "pip",
            "install",
        ] + get_torch_install_args(gpu)
        cmd_line(pip_cmd)
        if args.dry_run:
            skip("dry run")
        else:
            result = subprocess.run(pip_cmd)
            if result.returncode != 0:
                fail("PyTorch installation failed")
                sys.exit(1)
            ok("PyTorch installed")
            _run_torch_backend_check(conda, env_name, gpu)
        next_step += 1

    section(f"[{next_step}/{total_steps}] Installing analysis packages")
    pip_cmd = [
        conda,
        "run",
        "--no-capture-output",
        "-n",
        env_name,
        "pip",
        "install",
    ] + pip_packages
    print(f"  Packages: {', '.join(pip_packages)}")
    cmd_line(pip_cmd)
    if args.dry_run:
        skip("dry run")
    else:
        result = subprocess.run(pip_cmd)
        if result.returncode != 0:
            fail("Package installation failed")
            sys.exit(1)
        for pkg in pip_packages:
            ok(pkg)

    next_step += 1
    section(f"[{next_step}/{total_steps}] Running diagnostics")
    if args.dry_run:
        skip("dry run")
    else:
        _run_diagnostics(conda, env_name, diagnostics)

    elapsed = time.time() - t_start

    banner("Setup Complete")
    section("Summary")
    info("Environment", env_name)
    info("GPU backend", gpu_label(gpu) if install_torch else "not used")
    info("Python", args.python)
    torch_packages = 2 if install_torch else 0
    info("Packages", str(len(pip_packages) + torch_packages))
    info("Time", f"{elapsed:.0f}s")

    section("Next steps")
    print(f"  Activate:    conda activate {env_name}")
    print(f"  Check:       python setup_env.py --step {args.step} --check")
    print(f"  Remove:      python clean_env.py --step {args.step}")
    print()
    print("=" * WIDTH)


def clean_workflow_envs(*, workflow: str) -> None:
    """Remove the ZMART--<workflow>--* conda environments, or one of them
    with ``--step``. ``--dry-run`` lists them without removing anything."""
    parser = argparse.ArgumentParser(description=f"Remove conda envs for {workflow} workflow")
    parser.add_argument(
        "--step",
        default=None,
        help="Remove only this step's env. If omitted, removes all.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List matching environments without removing them",
    )
    args = parser.parse_args()

    prefix = f"ZMART--{workflow}--"
    t_start = time.time()

    banner("ZMART Analysis -- Environment Cleanup")

    section("Conda")
    try:
        conda_info = get_conda_info()
    except FileNotFoundError as e:
        fail(str(e))
        sys.exit(1)

    conda = get_conda_exe(conda_info)
    envs_dirs = conda_info.get("envs_dirs", [])

    info("Executable", conda)
    info("Envs directory", envs_dirs[0] if envs_dirs else "unknown")

    all_matching = list_envs_by_prefix(conda_info, prefix)
    if args.step:
        target_name = f"{prefix}{args.step}"
        targets = [target_name] if target_name in all_matching else []
        if not targets:
            section("Result")
            fail(f"Environment '{target_name}' not found")
            if all_matching:
                print()
                print("  Available environments:")
                for name in all_matching:
                    print(f"    {name}")
            sys.exit(1)
    else:
        targets = all_matching

    section("Environments found")
    if not targets:
        info("Matching", f"0 (pattern: {prefix}*)")
        banner("Nothing to remove")
        return

    info("Matching", str(len(targets)))
    for name in targets:
        print(f"    {name}")

    if args.dry_run:
        section("Result")
        skip(f"dry run -- {len(targets)} environment(s) would be removed")
        print()
        print("=" * WIDTH)
        return

    section("Removing")
    removed = 0
    for name in targets:
        cmd = [conda, "env", "remove", "-n", name, "-y"]
        cmd_line(cmd)
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            ok(name)
            removed += 1
        else:
            fail(name)

    elapsed = time.time() - t_start

    banner("Cleanup Complete")
    section("Summary")
    info("Removed", f"{removed}/{len(targets)} environment(s)")
    info("Time", f"{elapsed:.0f}s")

    if removed < len(targets):
        print()
        fail("Some environments could not be removed")

    print()
    print("=" * WIDTH)


def _warn_old_conda(conda_version: str) -> None:
    if conda_version == "unknown":
        return
    try:
        major, minor, *_ = conda_version.split(".")
        if float(f"{major}.{minor}") < 25.7:
            warn("Conda < 25.7 can be unreliable for env switching")
    except (ValueError, IndexError):
        return


def _run_diagnostics(conda: str, env_name: str, diagnostics: list[tuple[str, str]]) -> None:
    all_passed = True
    for label, code in diagnostics:
        result = subprocess.run(
            [
                conda,
                "run",
                "--no-capture-output",
                "-n",
                env_name,
                "python",
                "-c",
                code,
            ],
            capture_output=True,
            text=True,
        )
        output = result.stdout.strip()
        # A check reports a wrong answer by printing FAIL and exiting normally.
        if result.returncode == 0 and not output.startswith("FAIL"):
            ok(f"{label:<28s} {output}")
        else:
            fail(f"{label:<28s} {output}")
            lines = result.stderr.strip().splitlines()
            if lines:
                # The Python error, rather than conda's line saying the command failed.
                errors = [line for line in lines if re.match(r"\w+(Error|Exception)\b", line)]
                print("         " + (errors[-1] if errors else lines[-1]))
            all_passed = False

    if not all_passed:
        banner("Setup Failed")
        print("  Some diagnostics did not pass.")
        sys.exit(1)


def _run_torch_backend_check(conda: str, env_name: str, gpu: str) -> None:
    if gpu == "cpu":
        code = (
            "import torch; "
            "x = torch.ones((2, 2)); "
            "print(f'{torch.__version__} CPU tensor OK shape={tuple(x.shape)}')"
        )
    elif gpu == "mps":
        code = (
            "import torch; "
            "assert torch.backends.mps.is_available(), 'MPS is not available'; "
            "x = torch.ones((2, 2), device='mps'); "
            "print(f'{torch.__version__} MPS tensor OK device={x.device}')"
        )
    else:
        code = (
            "import torch; "
            "assert torch.cuda.is_available(), 'CUDA is not available'; "
            "x = torch.ones((2, 2), device='cuda'); "
            "torch.cuda.synchronize(); "
            "print(f'{torch.__version__} CUDA tensor OK device={x.device} "
            "name={torch.cuda.get_device_name(0)}')"
        )

    result = subprocess.run(
        [
            conda,
            "run",
            "--no-capture-output",
            "-n",
            env_name,
            "python",
            "-c",
            code,
        ],
        capture_output=True,
        text=True,
    )
    output = result.stdout.strip()
    if result.returncode == 0:
        ok(f"{'PyTorch backend check':<28s} {output}")
        return
    fail(f"{'PyTorch backend check':<28s}")
    if result.stderr.strip():
        print(result.stderr.strip())
    sys.exit(1)
