# V4 Engine Fix and Validation Report

Date: 2026-07-11

Target branch: `v4-engine`

Remote base reviewed: `5b98c65810ee6620a6e32b21fb49e1e4253ecf65`

Fix series reviewed:

- `f10baa1` — engine lifecycle/scope coordination fixes
- `96df3f3` — initial Fable verification notes
- `79648b6` — Claude engine/segmentation self-review fixes
- the follow-up changes described in this report

Reviewer handoff: Fable

## Scope and test-data constraint

This report covers a deep review and repair of the v4 engine and the object
analysis channel-axis contract. Validation used only repository fixtures,
temporary files, generated TIFF arrays, mock workers, and synthetic pipeline
steps. No production image or experiment dataset was read or modified.

The original change stopped short of production workflow changes. The later
review found that a test-only channel-axis change could not provide a usable
or safe contract, because the value also had to flow through pipeline input,
checkpoint identity, checkpoint reload, and CLI geometry. The follow-up was
therefore explicitly approved to make those narrowly scoped workflow changes.

## Original v4 fixes retained and revalidated

### Scope completion executor deadlock

Scope-completion coordination now runs in a dedicated executor instead of
occupying a phase-0 priority worker while waiting for phase-0 futures. This
removes the one-worker starvation/deadlock path.

### Non-waiting shutdown and worker-pool closure

`shutdown(wait=False)` cancels queued priority work, canceled submissions are
reflected in status, and closed worker pools reject late acquisition or return.
The scope executor also cancels queued completion work during non-waiting
shutdown.

### Accurate pending/running/failed status

Pipeline state records pending and running transitions explicitly. The failed
count is derived from the retained failure list so counters cannot drift after
scope failure collection.

### Cross-platform mock Cellpose test

The test PYTHONPATH uses `os.pathsep`, making it valid on Windows and POSIX.

## Claude changes reviewed

Claude's `79648b6` change correctly addressed these defects, and the behavior
was retained:

1. `WorkerTimeoutError` is distinct from `StepExecutionError`.
2. Conda workers receive an explicit engine PID for parent-heartbeat cleanup.
3. Duplicate pipeline names are checked again at insertion time.
4. Failed status is derived from the failure collection.
5. Ambiguous equal-end TIFF shapes are rejected unless `channel_axis` is
   explicit.

The review found three remaining implementation gaps and one reporting gap.
They are fixed below.

## Follow-up fixes

### 1. Atomic registration across duplicates and shutdown

Problem: the second duplicate-name check prevented two completed inserts, but
both callers could still parse and build the same pipeline concurrently. More
importantly, registration could finish successfully after shutdown had already
set the engine to non-accepting.

Fix:

- `_registering` reserves names under the engine lifecycle lock;
- duplicate registered or reserved names fail before YAML parsing;
- the final insertion rechecks `_accepting` under the same lock used by
  shutdown;
- reservations are released in `finally`, including parser/metadata failures;
- shutdown changes `_accepting` while holding that lock.

Deterministic tests cover same-name concurrency, shutdown while parsing, and
reservation cleanup after parse failure. Adversarial tests add 32 concurrent
same-name callers and 24 distinct registrations paused across shutdown.

### 2. Atomic submit acceptance across shutdown

Problem: submit checked `_accepting` outside the lifecycle lock. Shutdown could
close an executor after the check but before submission/accounting, producing
a partially counted or late submission.

Fix: acceptance, pipeline lookup, submission accounting, phase-0 executor
submission, and optional scope-coordinator submission now share the lifecycle
lock with shutdown. A submit either wins before shutdown or receives the
documented shutdown error; it cannot land between those states.

The existing submit/shutdown adversarial test now also asserts that no
unexpected exception type was hidden during the race.

### 3. End-to-end channel-axis contract

Problem: `segment_tiff()` accepted `channel_axis`, but the canonical detection
step never forwarded pipeline/YAML input. Checkpoint reload also ignored it,
and checkpoint identity did not include it. An explicit value supplied for an
ambiguous TIFF therefore still failed, while differently interpreted TIFFs
could share an identity hash.

Fix:

- `channel_axis` is read from pipeline input with YAML fallback;
- detection forwards it to `segment_tiff()`;
- it is included in segmentation identity and persisted checkpoints;
- reload uses the persisted axis before feature extraction;
- new checkpoints verify that their stored segmentation parameters still
  match their stored identity hash before those parameters are trusted;
- legacy checkpoints without the new key retain their prior hash behavior;
- axis `2` is normalized to `-1`, because both mean channel-last;
- booleans, floats, strings, middle axes, and other invalid declarations are
  rejected;
- the three object-analysis YAML pipelines expose `channel_axis: null`;
- the CLI exposes `--channel-axis` and uses it when calculating source image
  size; ambiguous shapes fail with an actionable CLI message.

Tests cover both orientations of `(3, 10, 3)`, exact pixel preservation,
checkpoint round-trip, checkpoint tampering, hash separation, alias
normalization, invalid values, CLI geometry, and property-style selection for
1, 2, 3, 5, and 8 channels.

### 4. Handoff report correction

The earlier report described only `f10baa1` and claimed production workflow
code had not changed even after Claude changed `_segmentation.py`. This report
supersedes that stale statement and describes the complete reviewed series.

## Validation

Environment:

- macOS
- CPython 3.11.15
- disposable venv at `/tmp/smart-analysis-review-venv`
- editable install from `.[test]`
- dependency compatibility check required before handoff

Commands:

```bash
python -m pytest engine/test_engine.py engine/test_lifecycle_adversarial.py \
  workflows/object_analysis/tests/test_segmentation.py \
  workflows/object_analysis/tests/test_channel_axis_adversarial.py \
  workflows/object_analysis/tests/test_object_analysis.py -q

python -m pytest -m adversarial --tb=short -q

python -m pytest \
  -m "not cellpose and not deep and not cluster and not conda_env" \
  --tb=short -q

python -m compileall -q engine workflows
uv pip check --python /tmp/smart-analysis-review-venv/bin/python
git diff --check
```

The CI-equivalent marker exclusion matches repository CI: the deselected tests
need real Cellpose/Torch/model access, a clustering environment, or dedicated
conda worker environments. Those resources are outside the approved synthetic
test-data scope.

### Local results

| Validation | Result |
| --- | --- |
| Targeted engine/object-analysis suites | 190 passed, 4 resource-marked skips |
| Full adversarial suite, including slow tests | 65 passed, 338 deselected |
| Registration/submit race set repeated 10 times | 40 passed |
| CI-equivalent suite | 392 passed, 11 deselected |
| Python compilation | passed |
| dependency compatibility (`uv pip check`) | passed |
| `git diff --check` | passed |

One CI-equivalent run emitted the existing scikit-image empty-region
`RuntimeWarning`; no test failed.

## Files changed in the complete reviewed series

Engine implementation and tests:

- `engine/__init__.py`
- `engine/_errors.py`
- `engine/_pipeline.py`
- `engine/_pool.py`
- `engine/_run.py`
- `engine/_worker.py`
- `engine/test_engine.py`
- `engine/test_lifecycle_adversarial.py`
- `engine/worker_script.py`

Object-analysis implementation, configuration, and tests:

- `workflows/_detection_checkpoint.py`
- `workflows/_segmentation.py`
- `workflows/basic_test/tests/test_adversarial.py`
- `workflows/object_analysis/pipelines/object_analysis.yaml`
- `workflows/object_analysis/pipelines/object_analysis_deep.yaml`
- `workflows/object_analysis/pipelines/object_detection.yaml`
- `workflows/object_analysis/run_pipeline.py`
- `workflows/object_analysis/steps/detect_objects.py`
- `workflows/object_analysis/steps/load_detected_objects.py`
- `workflows/object_analysis/tests/test_channel_axis_adversarial.py`
- `workflows/object_analysis/tests/test_object_analysis.py`
- `workflows/object_analysis/tests/test_segmentation.py`

Documentation:

- `docs/V4_Engine_Fix_Validation_Report.md`

## Fable verification checklist

1. Confirm registration reserves names before parsing and releases every
   reservation in `finally`.
2. Confirm shutdown and final registration insertion both use `_lock` while
   reading/writing `_accepting`.
3. Confirm submit acceptance and both executor submissions are atomic with
   shutdown under `_lock`.
4. Trace `channel_axis` from pipeline input/YAML through detection,
   segmentation identity, checkpoint JSON, and checkpoint reload.
5. Confirm `2` and `-1` produce the same identity while `0` differs.
6. Run the targeted, adversarial, and CI-equivalent commands above.
7. Confirm test inputs are temporary/synthetic and no production dataset is
   referenced.
8. Inspect the final GitHub Actions matrix and require every supported
   OS/Python job to pass.
