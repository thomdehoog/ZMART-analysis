Review the branch `cleanup/folder-layout` in https://github.com/thomdehoog/smart-analysis
against `release-candidate`. It is one commit, bd9a0c8. Do not push or merge anything; report findings only.

## What the commit claims to do

1. **Engine merge.** Nine files in `engine/` became three: `engine.py` (was `_pipeline.py` + `_loader.py`),
   `pipeline.py` (was `_run.py` + `ScopeError` from `_errors.py`) and `workers.py` (was `_worker.py` + `_pool.py`
   + the worker errors from `_errors.py`). `worker_script.py` and `conda_utils.py` stay. It is meant to be a pure
   move: no behaviour change, and `from engine import Engine, WorkerError, ..., ScopeError` works as before.
2. **Tests together.** `engine/test_*.py` moved to a top-level `tests/` folder (`test_lifecycle_adversarial.py` →
   `test_engine_shutdown.py`), plus `workflows/focus/tests/test_image_io.py` → `tests/test_image_io.py`.
   `pyproject.toml` `testpaths` is now `["tests", "workflows"]`.
3. **Environment scripts de-duplicated.** Each `workflows/*/environments/setup_env.py` and `clean_env.py` had its
   own copy of ~400 lines of helpers. They now live once in `engine/conda_utils.py` (`setup_workflow_env`,
   `clean_workflow_envs`). `setup_workflow_env` gained a `steps_dir` argument that fills in `__STEPS__` in the
   diagnostic one-liners. The copy kept is the one from the setup scripts (it has `--override-channels -c
   conda-forge`); the older copies in the clean scripts were never called.
4. **Shared helpers.** `workflows/_image_io.py`, `_focus_metrics.py` and `_population.py` moved to
   `workflows/shared/` (with an `__init__.py`). Imports changed from `from _image_io import` to
   `from shared.image_io import`. In `focus_metrics.py`, `_brenner`, `_dct_entropy`, `_vollath_f4` and
   `_intensity` lost their leading underscore.
5. **Docs and leftovers.** `workflows/focus/FOCUS_METRICS_OPTIONS.md` became `workflows/focus/README.md`, rewritten.
   The repo-root `__init__.py`, `workflows/__init__.py` and `workflows/driver_configuration/__init__.py` were
   deleted (they came from when this code was copied into ZMART). `docs/folder-structure.md`, `docs/writing-a-step.md`
   and the README link were updated.

## What to check, most important first

- **The merge really is a pure move.** Diff the bodies of `engine.py`, `pipeline.py` and `workers.py` against the
  old files (`git show release-candidate:engine/_run.py` etc.). Look for dropped or duplicated code, a lost
  import, a module-level name that now clashes with another (two `logger`s, constants, helper functions with the
  same name in `_worker.py` and `_pool.py`), and anything that depended on its module's `__name__` or `__file__`.
  `workers.py` uses `ENGINE_DIR = Path(__file__).parent` to find `worker_script.py`; confirm that still resolves.
- **Tests that patch module attributes.** Tests patch `engine.engine.parse_yaml` and `workers.subprocess.Popen`.
  Confirm each patch hits the name the code actually looks up at run time.
- **Steps running inside a worker.** Steps do `sys.path.insert(0, <workflows>)` and then `from shared.X import`.
  Could a package called `shared` already installed in a user's conda environment shadow ours? Is
  `workflows/shared/__init__.py` enough to make ours win? Check `engine/worker_script.py`: it loads steps by path,
  so confirm nothing there assumed the old `_image_io` names.
- **Environment scripts.** Run each `setup_env.py --help` and `clean_env.py --help` from outside the repo.
  `setup_env.py` must still import `conda_utils` by path, so `conda_utils.py` has to keep importing only the
  standard library; check that it does. It now has `from __future__ import annotations`; make sure nothing else
  needs to come before it. Check that `object_analysis/environments/setup_env.py` still has its three profiles
  (`cellpose`, `classical`, `umap`) and parses `--step` before `setup_workflow_env` parses it again.
- **Deleted `__init__.py` files.** Removing the root `__init__.py` changes how pytest imports `conftest.py` and the
  test modules (rootdir-based versus package-based import). Confirm that no two test files now share a basename
  across `tests/` and `workflows/*/tests/`, which would make pytest refuse to collect them. Also check that nothing
  in ZMART-microscopy (https://github.com/thomdehoog/ZMART-microscopy, branch `release-candidate`) imports
  `zmart_analysis.*`, `workflows.*` or anything from the engine besides `Engine`. Its `workflows/target_acquisition/workflow/steps.py`
  finds the engine by looking for an `engine/` folder and then does `import engine`.
- **Stale references.** Search the whole repo, including `.github/workflows/test.yml`, `CITATION.cff` and the
  docs, for `_errors`, `_loader`, `_run`, `_pipeline`, `_pool`, `_worker`, `_image_io`, `_focus_metrics`,
  `_population`, `FOCUS_METRICS_OPTIONS` and `engine/test_`.
- **Docs.** `workflows/focus/README.md` should agree with `steps/score_focus.py` and `pipelines/focus.yaml`
  (defaults: `metric: brenner`, `intensity_percentile: 99`, `skip_ends: 2`). `docs/folder-structure.md` should
  match the real tree. This repo's CLAUDE.md asks for docs written for biologists: plain, complete sentences,
  and any jargon explained.

## How it was tested

`pytest -m "not cellpose and not conda_env and not pooch"` gave 343 passed, 4 skipped, before and after the change.
The only failures were 13 in `tests/test_conda_utils.py`, identical before and after, because the test machine has
no conda. Please run the same command on a machine with conda. If you can, also run
`python workflows/focus/environments/setup_env.py --dry-run` there, and the `cellpose` and `conda_env` tests.

## Known, not from this commit

pyflakes reports that `_none_or_int` and `_none_or_float` are each defined twice in
`workflows/object_analysis/steps/detect_objects.py` (lines ~323/1033 and ~329/1037). That was already the case on
`release-candidate`. Say whether the two copies differ, and which one wins.

## What to report

For each finding, give the file and line, what goes wrong and in what situation, and how sure you are. Keep
real bugs separate from style. If you find nothing wrong in an area, say which areas you checked.
