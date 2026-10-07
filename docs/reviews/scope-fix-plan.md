# Plan: fixing analysis over scopes

Branch: `claude/scope-functionality-tests`. The tests are in `tests/test_scopes.py`.
Revised after two reviews, one of the engine and one of the workflows, tests and docs.

**Status: implemented**, one commit per section below, all six decisions taken as
recommended. Kept as the record of what was wrong and why it was fixed this way.

## Where we stand

Analysis over scopes works for the common cases. `tests/test_scopes.py` checks 15 of them:
three levels at once, two carriers acquired together, the same tiles summarised at
another level, chained signals, `all`, failed tiles. Three gaps are marked
`xfail(strict=True)`; each fix below removes a marker.

What is wrong today, confirmed against the code:

| Problem | What happens now |
|---|---|
| A failed compartment step | Its carrier is not told; it sees one compartment fewer. The failure is recorded as `phase_1`, and the *next* close of that compartment hands it to a step as a tile failure. |
| A carrier whose compartments all failed | The carrier step never runs. Only a log warning. |
| Provenance | Every result records its own step only. A carrier cannot be traced back to its tiles, or to where they ran. |
| Signals out of order | A carrier close waits on compartment closes sent *after* it. With enough carriers waiting, the signal threads fill up and the engine hangs. |
| A recipe that skips a level | With `scope: group` + `scope: carrier` and no compartment phase, the unit of a group drops `compartment`. Group 1 of compartment 1 and group 1 of compartment 2 are the same unit; one swallows the other's tiles. |
| A compartment never closed | When its carrier closes, its tiles are dropped from the job list but stay in memory, and nobody is told. |
| `shutdown(wait=False)` | Cancels queued signal chains whose running marks are already set; a thread waiting on them hangs for ever. |
| `ScopeError` | Documented, never raised. A `complete="compartment"` whose scope leaves out `compartment` quietly closes every compartment of every carrier. |
| Results do not name their unit | A carrier result's `_scope` is the closing submit's scope, so it still says `compartment: 96`. |
| `compare_populations` | Ignores `failures`. |
| Docstrings | `Engine.register` says the YAML may name an environment; it may not. `pipeline.py` repeats its introduction and has an empty `ScopeError` heading. `test_engine.py` says "single-axis". |

## Decisions

Each has a recommendation. Nothing starts until these are agreed.

1. **One ordering rule.** A close signal with submission index `S` waits on, collects and
   reports only entries with index `< S`. Tiles, unit results and failures all carry the
   index. Without this a carrier's contents depend on which compartment finished first.
   *Recommended: yes.*

2. **Leftover tiles are reported, not freed.** When a carrier closes, the tiles of a
   compartment that has not closed are reported to the carrier step as failures and listed
   in `status()`, but kept, so a compartment signal that comes later still finds them. The
   engine cannot tell "never closed" from "not yet closed". *Recommended: report.*

3. **A skipped level.** How does a group identify itself in a recipe without a compartment
   phase? Options: (a) the recipe lists its levels, widest first, in
   `metadata: levels: [carrier, compartment, group]`, and the unit keeps every wider level
   whether or not it is a phase; (b) document that ids must be unique within the wider
   levels the recipe *has*. *Recommended: (a).* It is one line in the recipe, it fixes a
   real bug, and it gives the engine the order it needs to name units and files widest
   first. A recipe without `levels` keeps today's rule.

4. **Lineage.** Every scoped result carries a `lineage`: the tiles under it, the failures
   under it, and where every step below it ran (fix 6). *Recommended: yes.*

5. **`ScopeError`.** `submit` raises it when `complete` names a level that is a scope in
   the recipe, the scope leaves out that key, and an earlier submit to the pipeline used
   it. The first bad submit of a pipeline still slips through. *Recommended: yes, and say
   so in the docs.*

6. **Failures in `status()`.** Today a failure leaves `status()["failures"]` once the next
   level claims it. Keep that rule: the failure then lives in the claiming result and its
   `lineage`. Failures of the last phase stay for the engine's life. *Recommended: keep,
   document.*

## The work, one commit each

Every commit: run `pytest -m "not cellpose and not conda_env and not pooch"`, and
`tests/test_scopes.py` ten times in a loop.

### 0. Test harness

- Add `pytest-timeout` to `[test]` and `timeout = 120` to `[tool.pytest.ini_options]`. A
  deadlock otherwise hangs a CI job for six hours, because `with Engine()` waits for its
  threads.
- `scoped()` in `test_scopes.py` raises on timeout, with what it collected, instead of
  returning a partial list.
- Replace the 5 s timeout in `test_a_carrier_of_failed_compartments_still_runs` with the
  default; two cold workers on Windows take 2-4 s to spawn.
- `test_a_failed_tile_reaches_its_compartment_and_no_other` polls `status` instead of
  reading it once.

### 1. Order and the deadlock

`begin_scoped` stores `(scope, submission_idx, Event)`. `_wait_for_scoped` waits only on
entries with a lower index. `get_matching_futures`, `collect_for_scope` and
`_collect_phase0` select by index too. `store_phase_result` and `record_failure` store the
index. A done-callback on the chain future calls `end_scoped` for every token when the
future is cancelled, so `shutdown(wait=False)` cannot leave a waiter hanging.

Tests:
- Out of order, deterministic: a tile step that blocks on a file until the test releases
  it, so the carrier handler is still waiting on tiles when the compartment signal is
  submitted; `max_concurrent=1`. Assert what the carrier collected, not only that it ran.
- `shutdown(wait=False)` with a waiting carrier returns.

### 2. Failures reach every level

- A failure record is `{scope, step, error, phase, submission_idx}`. `record_cancellation`
  tags phase 0. `_execute_scoped_phase` tracks the step name, as phase 0 does, so a failed
  scoped step is recorded under its own name.
- One helper, `_take_failures(phase_idx, value, before)`, serves every phase: the failures
  of the previous phase, of this unit, with a lower index. `all` no longer sweeps failures
  from every phase.
- A unit with failures and no results runs and is told. A unit with neither still only
  warns.

Tests: remove the two markers; a failure names its real step; a failed compartment step is
*not* handed to the next close of that compartment; `all` gets only the previous phase's.

### 3. Units, levels and leftovers

- `metadata: levels:` is read at `register`; `scope_key` uses it when present (decision 3).
  The engine orders every unit widest first.
- `pipeline_data["metadata"]["unit"]` is set on every scoped run; `{}` for `all`.
- On closing a level, tiles and unit results of lower levels that belong to this unit but
  were not closed are reported to the step as failures (`step: "engine"`,
  `error: "compartment not closed"`) and kept (decision 2).
- `status()` gains `held: {"results": n, "units": [...]}` so an aborted acquisition is
  visible.

Tests: interleaved groups of two compartments in a `group`/`carrier` recipe stay apart;
a carrier closed with one compartment open reports it, and the compartment closed later
still runs; `held` counts and empties.

### 4. `ScopeError`

The check runs in `submit` before the tile is queued, and for a list of levels before any
token is registered. Seen keys are kept per pipeline under its lock. `all` and unknown
levels stay accepted.

Tests: raised; `all` and an unknown level accepted; a refused submit does not run its tile.

### 5. Lineage

Built by the engine before a scoped step runs, attached to the result after it returns, so
a step that builds a new dict cannot drop it.

```python
result["lineage"] = {
    "submissions": [0, 1, 2, 5],       # every tile under this unit
    "failed": [{...failure record}],   # every failure under it, tiles and units
    "provenance": {                    # every step below, distinct records
        "detect_objects": [{"environment": ..., "python": ..., "fingerprint": ..., "packages": ...}],
        "summarise_population": [{...}],
    },
}
```

A tile contributes its index and `provenance`; a unit contributes its `lineage` and
`provenance`. Records are deduplicated by equality of the whole record; two is a sign the
environment changed during the run. Not mutated after attaching, because `publish_result`
copies shallowly.

Tests: remove the marker; three levels; a failed tile and a failed compartment both appear
in `failed`.

### 6. Workflow steps, docs, wording

Steps:
- Both steps read `metadata["unit"]`, falling back to `scope` when it is absent, so hand-
  built metadata in `test_population_steps.py` and `docs/2` keeps working. `LEVELS` and
  `_unit_of` go; `unit_name` keeps the engine's order.
- `compare_populations` reports `n_failed_compartments`, counting only failures whose
  `phase` is the compartment phase.
- `object_analysis_scoped.yaml` gets `levels: [carrier, compartment, group]`.
- End-to-end test of the scoped recipe in CI: `monkeypatch.setenv("CONDA_DEFAULT_ENV",
  "ZMART--object_analysis--classical")` before `Engine()`, so the steps' environment maps
  to the test interpreter; a generated copy of the YAML with `detect_objects_fast` and
  small synthetic tiles; `extras` without the texture crops. Two carriers, two
  compartments each, one failed tile.

Docs:
- `docs/1_use_the_engine`: `levels`; the ordering rule in one sentence ("send a
  compartment's `complete` before its carrier's; a carrier does not wait for signals sent
  after it"); failures and lineage; `held`; `ScopeError` as it now works, with the
  first-submit hole; the `status` rule (decision 6); a late tile waits until its unit is
  closed again; one name for the collect-everything level (`all`, not `experiment`).
- `docs/2_implement_an_analysis_step`: what a scoped step receives: `metadata["unit"]`,
  the failure record's fields, `lineage`.
- `object_analysis_scoped.yaml` header and the workflow README: `levels`, failures,
  lineage, in a line or two.
- `engine/pipeline.py` docstring: remove the repeated introduction and the empty heading;
  one line on how failures and lineage move up. `Engine.register`: drop "unless the YAML
  names one for it". `tests/test_engine.py`: "scope completion"; point to `test_scopes.py`.
  Remove the unused `level` argument from the three `PipelineState` methods.

## Not in this plan

- Dropping late tiles instead of keeping them.
- Freeing held tiles automatically. Both would be a separate decision.
