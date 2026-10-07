# Plan: fixing analysis over scopes

Branch: `claude/scope-functionality-tests`. The tests are in `tests/test_scopes.py`.

## Where we stand

Analysis over scopes works for the common cases. The tests check 15 of them:

- three levels at once
- two carriers acquired together
- the same tiles summarised at another level
- chained signals
- `all`
- failed tiles

Three gaps are marked `xfail(strict=True)`. Each fix below removes a marker.

| Gap | What happens now |
|---|---|
| A failed compartment step | Its carrier is not told. It just sees one compartment fewer. |
| A carrier whose compartments all failed | The carrier step never runs. There is only a log warning. |
| Provenance | Every result records its own step only. A carrier cannot be traced back to its tiles, or to where they ran. |

A review of the code found six more problems:

- A possible deadlock when signals arrive out of order.
- Compartments that never close are lost silently.
- `ScopeError` is never raised.
- A carrier result does not name its unit.
- `compare_populations` ignores failures.
- Some docstrings are out of date.

## Decisions needed first

1. **Lineage.** Attach a `lineage` to every scoped result, in the shape shown in fix 5.
2. **`ScopeError`.** Raise it only when `complete` names a level whose key this scope leaves out, and earlier submits used that key.
3. **Failures in `status`.** A failure leaves `status()["failures"]` once the next level claims it. That is the current rule, and the plan keeps it and documents it. The failure stays visible in the claiming result and its `lineage`.

## The work, one commit each

### 1. Fix the deadlock on out-of-order signals

**Problem.** A carrier signal waits on every compartment step of its carrier that is still running. That includes compartment signals submitted *after* the carrier signal. Those are queued behind it on the same pool of signal threads. When enough carriers wait like this, the engine hangs.

**Fix.** `begin_scoped` gives each signal a sequence number. `_wait_for_scoped` waits only on earlier signals.

**Test.** Use `max_concurrent=1`. Send a carrier signal before its last compartment signal. Check that the run finishes.

### 2. Hand failures on to every level

**Problem.** Failures sit in one flat list. Only the first scoped phase takes from it (`_collect_phase0`). Later phases always get `[]` (`engine/pipeline.py`, `collect_for_scope`).

**Fix.**
- `record_failure` stores the phase the failure happened in.
- One helper, `_take_failures(phase_idx, value)`, serves every phase. It takes the failures of the previous phase that belong to the unit being closed.
- An `all` collection then no longer sweeps up failures from every phase.
- A failed scoped step is recorded under its own name (`compare_populations`), not `phase_1`.
- Gap 2 follows from this. A carrier whose compartments all failed now has failures, so it runs and is told.

**Tests.**
- Remove the markers from the two failure tests.
- Add a test that a failure names its real step.
- Add a test that `all` gets only the failures of the phase before it.

### 3. Report compartments that never closed

**Problem.** When a carrier closes, tiles of a compartment that never got `complete="compartment"` are never collected or reported. They stay in memory.

**Fix.** On closing a level, the engine:

- takes the leftover tiles of that unit,
- hands them to the step as failures ("compartment never closed"),
- frees them.

**Test.** Close a carrier with one compartment left open. The carrier step reports it.

### 4. Raise `ScopeError` for a missing key

**Problem.** `complete="compartment"` with a scope that leaves out `compartment` falls through to "collect everything". It closes every compartment of every carrier. `ScopeError` is documented but nothing raises it.

**Fix.** `submit` raises `ScopeError` straight away, on the caller's thread, when all of these hold:

- `complete` names a level that is a scope in this recipe,
- the submit's scope does not contain that key,
- earlier submits to this pipeline used that key.

`all` keeps working, because no job carries an `all` key. Closing a level the recipe does not have stays a warning. That way one acquisition script can drive several recipes.

**Tests.**
- The error is raised.
- `all` and an unknown level are still accepted.

### 5. Lineage and unit on every scoped result

**Problem.** A step that builds a new dict, such as `summarise_population`, drops everything about its inputs. A carrier result's `_scope` is the scope of the submit that closed it, so it still says `compartment: 96`.

**Fix.** Before running a scoped phase, the engine builds the lineage from the phase's inputs. It sets the unit on the metadata and attaches the lineage to the result:

```python
pipeline_data["metadata"]["unit"] = {"carrier": 1}   # from scope_key

result["lineage"] = {
    "submissions": [0, 1, 2, 5],     # every tile under this unit
    "failed": [3],                   # every tile that failed under it
    "provenance": {                  # every step below this one, distinct records
        "detect_objects": [{"environment": ..., "python": ..., "fingerprint": ..., "packages": ...}],
        "summarise_population": [{...}],
    },
}
```

- A tile input contributes its `submission_idx` and `provenance`.
- A scoped input contributes its own `lineage` and `provenance`.
- One record per step is normal. Two records mean the environment changed during the run.
- Each step's own `provenance` is unchanged.

**Tests.**
- Remove the marker from the trace test.
- Check `lineage` across three levels.
- Check that a failed tile shows up in `failed`.

### 6. Workflow steps, wording and docs

**Workflow steps.**
- `compare_populations` reports `n_failed_compartments`, as `summarise_population` reports `n_failed_tiles`.
- Both steps read `metadata["unit"]`. The copies of `LEVELS` and `_unit_of` go.
- Add a case to `test_population_steps.py`.
- Add an end-to-end test of `object_analysis_scoped.yaml`. It swaps in the fast detection step, so it needs no Cellpose and runs in CI.

**Engine wording.**
- `engine/pipeline.py` module docstring: remove the repeated introduction and the empty `ScopeError` heading. Add one line on how failures and lineage move up the levels.
- `Engine.register` docstring: remove "unless the YAML names one for it". The YAML cannot name a step's environment.
- `tests/test_engine.py` docstring: "single-axis completion" becomes "scope completion". Point to `test_scopes.py` for the multi-level cases.
- Remove the unused `level` argument from `get_matching_futures`, `cleanup_consumed_entries` and `_collect_phase0`.

**Docs.**
- `docs/1_use_the_engine`: describe `ScopeError` as it now works. Add a short paragraph on failures and lineage.
- `object_analysis_scoped.yaml` header and `workflows/object_analysis/README.md`: the same, in a line or two.
- Say plainly that a tile arriving after its unit closed waits until that unit is closed again.

## Not in this plan

Late tiles are kept until their unit closes again. This is documented behaviour. Dropping them instead, with a failure record, would be a separate decision.

## Before pushing

- Run the full suite with the CI filter: `pytest -m "not cellpose and not conda_env and not pooch"`.
- Run `tests/test_scopes.py` several times in a row to catch timing problems.
- The new tests depend on timing, and the CI matrix includes Windows and macOS. Watch the first CI run.
