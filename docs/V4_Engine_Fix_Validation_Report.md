# V4 Engine Fix and Validation Report

Date: 2026-07-11

Branch: `v4-engine`

Remote base reviewed: `5b98c65810ee6620a6e32b21fb49e1e4253ecf65`

Reviewer handoff: Fable

## Purpose

This change set fixes engine concurrency, shutdown, status-reporting, and
cross-platform CI problems found during a review of the v4 branch. Validation
used only repository test fixtures and synthetic test data. No production
workflow or production step implementation was changed.

## Problems and fixes

### 1. Scope completion could deadlock the priority executor

Root cause: scope-completion work ran in the same priority executor as phase-0
jobs and synchronously waited for matching phase-0 futures. With one executor
thread, a completion task could run ahead of lower-priority phase-0 work and
occupy the only thread needed to complete that work.

Fix: scope completion now runs in a separate `ThreadPoolExecutor`. The priority
executor remains responsible for phase-0 jobs, so a completion waiter cannot
consume phase-0 execution capacity.

Primary code: `engine/_pipeline.py`

Regression test:
`TestEnginePriority.test_scope_completion_does_not_block_lower_priority_phase0`

### 2. `shutdown(wait=False)` could run queued work and leave a worker alive

Root cause: non-waiting shutdown marked the priority executor as closed but
allowed it to drain queued tasks. The worker pool was shut down immediately,
so later queued tasks could create a new worker after shutdown had returned.

Fix:

- non-waiting priority-executor shutdown cancels queued futures;
- canceled submissions are reflected in pipeline status;
- both the top-level worker pool and each environment pool have a closed state;
- closed pools reject acquisition and do not return workers to the idle list;
- scope-executor shutdown cancels queued completion work when `wait=False`.

Primary code: `engine/_pipeline.py`, `engine/_pool.py`, `engine/_run.py`

Regression tests:

- `TestEngineLifecycle.test_shutdown_without_wait_cancels_queue_and_closes_workers`
- `TestPool.test_shutdown_before_use`

### 3. `status()` always reported `running=0`

Root cause: running state was a hard-coded approximation and pending state was
derived from cumulative completion and failure counters.

Fix: `PipelineState` now tracks pending and running operations explicitly.
Transitions are recorded when submissions start, finish, fail, or are canceled.

Primary code: `engine/_run.py`, `engine/_pipeline.py`

Regression test:
`TestEngineStatus.test_status_tracks_pending_and_running_jobs`

### 4. Mock Cellpose CI test failed on Linux and macOS

Root cause: the test built `PYTHONPATH` with a hard-coded semicolon. Semicolon is
the Windows separator; POSIX platforms require a colon.

Fix: the test now uses `os.pathsep`.

Primary test:
`workflows/object_analysis/tests/test_object_analysis.py::test_split_detection_features_engine_path_with_mock_backend`

## Validation performed

All validation used a disposable Python 3.11 environment installed from
`.[test]` and repository-provided test/synthetic inputs.

| Validation | Result |
| --- | --- |
| Focused engine regressions | 4 passed |
| Former cross-platform mock Cellpose failure | 1 passed |
| Engine suite excluding slow/conda-env tests | 95 passed, 1 deselected |
| Fast adversarial suite | 22 passed |
| Slow adversarial suite | 12 passed |
| CI-equivalent suite | 345 passed, 11 deselected |
| Python compilation | passed |
| `pip check` | passed |
| `git diff --check` | passed |

CI-equivalent command:

```bash
pytest -m "not cellpose and not deep and not cluster and not conda_env" --tb=short -q
```

The 11 deselected tests require external runtime resources that the repository's
GitHub Actions workflow also does not provision: real Cellpose/Torch, DINO model
access, clustering environments, or dedicated conda test environments.

## Files intentionally changed

- `engine/_pipeline.py`
- `engine/_pool.py`
- `engine/_run.py`
- `engine/test_engine.py`
- `workflows/object_analysis/tests/test_object_analysis.py`
- `docs/V4_Engine_Fix_Validation_Report.md`

## Constraint and residual issue

The review also identified ambiguous channel-axis inference in
`workflows/_segmentation.py`. It was not changed because the requested scope
explicitly prohibited changes to production workflow and step scripts. A future
fix should add an explicit channel-axis contract or reject ambiguous shapes.

## Suggested Fable verification

1. Inspect the separate scope executor and confirm it is shut down before the
   worker pool.
2. Confirm non-waiting priority shutdown cancels queued futures.
3. Confirm worker acquisition fails after pool shutdown.
4. Run the four focused regressions named above.
5. Run the CI-equivalent command and verify all selected tests pass.
6. Confirm `git diff <base>..HEAD -- workflows` contains only the test-file
   portability change and no production workflow or step change.

## Fable verification results

Date: 2026-07-11

Verified commit: `f10baa1` against base `5b98c65`. Validation used a
disposable Python 3.11.15 venv installed from `.[test]` (`pip check` clean)
on Linux.

1. Confirmed. `Engine` creates a dedicated `ThreadPoolExecutor`
   (`_scope_executor`, `engine/_pipeline.py`) for scope-completion work,
   separate from the phase-0 `_PriorityThreadPool`. `Engine.shutdown()`
   closes the priority executor, then the scope executor (with
   `cancel_futures=not wait`), then the worker pool.
2. Confirmed. `_PriorityThreadPool.shutdown(wait=False)` drains the heap
   under the lock and cancels every queued future; canceled submissions are
   reflected in status via a done-callback that decrements the pending
   counter (`engine/_run.py`).
3. Confirmed. Both `WorkerPool` and `_EnvPool` set a closed flag on
   shutdown; `_EnvPool.acquire()` and `WorkerPool._get_env_pool()` raise
   `RuntimeError` after close, and `_EnvPool.release()` no longer returns
   workers to the idle list once closed.
4. Passed. All four focused regressions plus the mock-Cellpose pathsep test
   passed (5 passed).
5. Passed with an environment caveat. The CI-equivalent command selected
   345 tests and deselected 11, matching the report. 332 passed; the 13
   failures were all in `engine/test_conda_utils.py` with
   `FileNotFoundError: Could not run 'conda info'` — conda is not installed
   in the verification container. CI provisions Miniconda via
   `setup-miniconda`, so these are environment-availability failures, not
   code defects. No non-conda test failed.
6. Confirmed. `git diff 5b98c65..f10baa1 -- workflows` contains only the
   `os.pathsep` portability change in
   `workflows/object_analysis/tests/test_object_analysis.py`. The full
   change set touches exactly the files listed under "Files intentionally
   changed". `python -m compileall engine workflows` and
   `git diff 5b98c65..f10baa1 --check` both passed.
