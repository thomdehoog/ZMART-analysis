"""
Analysis over scopes, end to end.

A sample is divided into levels, narrowest first: tile, group, compartment,
carrier. Each test runs a real recipe through the engine and checks what
every level receives: which tiles, which failures, which provenance.

The steps are deliberately small. ``tile`` passes a number on; ``unit``
adds up whatever the level below it handed in. So every expected value
can be worked out by hand.

Usage
-----
    python -m pytest tests/test_scopes.py -v
"""

import textwrap
import time

import pytest

from engine import Engine

pytestmark = pytest.mark.integration

LEVELS = ["group", "compartment", "carrier"]

TILE = """
    def run(pd, state, **p):
        if pd["input"].get("fail"):
            raise ValueError("tile failed")
        pd["value"] = pd["input"]["value"]
        return pd
"""

# Adds up its inputs and names the unit it closed. A value of 666 breaks it.
UNIT = """
    LEVELS = ["group", "compartment", "carrier"]

    def run(pd, state, **p):
        meta = pd["metadata"]
        values = [r.get("value", r.get("total")) for r in pd["results"]]
        if 666 in values:
            raise ValueError("unit failed")
        level = meta["scope_level"]
        wider = LEVELS[LEVELS.index(level):] if level in LEVELS else []
        return {
            "total": sum(values),
            "n_inputs": len(values),
            "n_failures": len(pd["failures"]),
            "level": level,
            "unit": {k: meta["scope"][k] for k in wider if k in meta["scope"]},
        "engine_unit": meta["unit"],
            "inputs_provenance": [sorted(r.get("provenance", {})) for r in pd["results"]],
        }
"""

# A second step in the same scoped phase.
DOUBLE = """
    def run(pd, state, **p):
        pd["doubled"] = pd["total"] * 2
        return pd
"""


@pytest.fixture(scope="module")
def steps_dir(tmp_path_factory):
    folder = tmp_path_factory.mktemp("scope_steps")
    for name, code in {"tile": TILE, "unit": UNIT, "double": DOUBLE}.items():
        (folder / f"{name}.py").write_text(textwrap.dedent(code))
    return folder


@pytest.fixture
def recipe(steps_dir):
    """recipe("compartment", "carrier") -> tile, then one unit step per scope."""
    def build(*scopes, then_double=False, levels=None):
        lines = [f'metadata:\n  functions_dir: "{steps_dir.as_posix()}"']
        if levels:
            lines.append(f"  levels: [{', '.join(levels)}]")
        lines += ["wf:", "  - tile:"]
        for scope in scopes:
            lines += ["  - unit:", f"      scope: {scope}"]
        if then_double:
            lines.append("  - double:")
        path = steps_dir / f"recipe_{'_'.join(scopes)}_{then_double}_{bool(levels)}.yaml"
        path.write_text("\n".join(lines) + "\n")
        return str(path)
    return build


def scoped(engine, expected, timeout=30):
    """Poll until *expected* scoped results arrived; return all results."""
    collected = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        collected += engine.results("p")
        if sum(r["_phase"] > 0 for r in collected) >= expected:
            return collected
        time.sleep(0.05)
    raise TimeoutError(
        f"{expected} scoped results expected within {timeout}s, got "
        f"{sum(r['_phase'] > 0 for r in collected)}; status {engine.status('p')}"
    )


def status_when(engine, condition, timeout=30):
    """Poll ``engine.status`` until *condition* holds; return the status."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        status = engine.status("p")
        if condition(status):
            return status
        time.sleep(0.05)
    return engine.status("p")


def by_unit(results, level):
    """Scoped results of one level, keyed by their unit as a sorted tuple."""
    return {tuple(sorted(r["unit"].items())): r for r in results if r.get("level") == level}


def submit_tiles(e, values, scope, complete=None):
    """Submit one tile per value; the last one carries the completion signal."""
    for i, value in enumerate(values):
        last = i == len(values) - 1
        e.submit("p", {"value": value}, scope=scope, complete=complete if last else None)


# -- Levels ------------------------------------------------------------


def test_three_levels_each_add_up_their_own_units(recipe):
    """tile -> group -> compartment -> carrier, two of each."""
    with Engine() as e:
        e.register("p", recipe("group", "compartment", "carrier"))
        for carrier in (1, 2):
            for compartment in (1, 2):
                for group in (1, 2):
                    closes = ["group"]
                    if group == 2:
                        closes.append("compartment")
                    if group == 2 and compartment == 2:
                        closes.append("carrier")
                    value = 1000 * carrier + 100 * compartment + 10 * group
                    submit_tiles(e, [value, value + 1],
                                 {"carrier": carrier, "compartment": compartment, "group": group},
                                 complete=closes)
        results = scoped(e, 8 + 4 + 2)

    groups = by_unit(results, "group")
    compartments = by_unit(results, "compartment")
    carriers = by_unit(results, "carrier")
    assert len(groups) == 8 and len(compartments) == 4 and len(carriers) == 2

    assert groups[(("carrier", 2), ("compartment", 1), ("group", 2))]["total"] == 2120 + 2121
    assert compartments[(("carrier", 1), ("compartment", 2))]["total"] == 1210 + 1211 + 1220 + 1221
    assert compartments[(("carrier", 1), ("compartment", 2))]["n_inputs"] == 2
    for c in (1, 2):
        every_tile = [1000 * c + 100 * m + 10 * g + d for m in (1, 2) for g in (1, 2) for d in (0, 1)]
        assert carriers[(("carrier", c),)]["total"] == sum(every_tile)
    assert carriers[(("carrier", 2),)]["n_inputs"] == 2


def test_two_carriers_interleaved_keep_their_compartments_apart(recipe):
    """Both carriers have a compartment 1; tiles of the two arrive mixed."""
    with Engine(max_concurrent=4) as e:
        e.register("p", recipe("compartment", "carrier"))
        for value in range(3):
            e.submit("p", {"value": 1}, scope={"carrier": 1, "compartment": 1})
            e.submit("p", {"value": 100}, scope={"carrier": 2, "compartment": 1})
        e.submit("p", {"value": 0}, scope={"carrier": 2, "compartment": 1},
                 complete=["compartment", "carrier"])
        e.submit("p", {"value": 0}, scope={"carrier": 1, "compartment": 1},
                 complete=["compartment", "carrier"])
        results = scoped(e, 4)

    carriers = by_unit(results, "carrier")
    assert carriers[(("carrier", 1),)]["total"] == 3
    assert carriers[(("carrier", 2),)]["total"] == 300


def test_a_wider_level_closed_by_a_later_submit_waits_for_the_narrower(recipe):
    """The carrier is closed by its own submit, after slow compartments."""
    with Engine() as e:
        e.register("p", recipe("compartment", "carrier"))
        for compartment in (1, 2, 3):
            submit_tiles(e, [compartment] * 4, {"carrier": 1, "compartment": compartment},
                         complete="compartment")
        e.submit("p", {"value": 0}, scope={"carrier": 1}, complete="carrier")
        results = scoped(e, 4)

    carrier = by_unit(results, "carrier")[(("carrier", 1),)]
    assert carrier["n_inputs"] == 3
    assert carrier["total"] == 4 * (1 + 2 + 3)


def test_all_after_carriers_collects_every_carrier(recipe):
    with Engine() as e:
        e.register("p", recipe("compartment", "carrier", "all"))
        submit_tiles(e, [1, 2], {"carrier": 1, "compartment": 1}, complete=["compartment", "carrier"])
        submit_tiles(e, [10, 20], {"carrier": 2, "compartment": 1},
                     complete=["compartment", "carrier", "all"])
        results = scoped(e, 5)

    everything = [r for r in results if r.get("level") == "all"]
    assert len(everything) == 1
    assert everything[0]["total"] == 33
    assert everything[0]["n_inputs"] == 2


def test_a_compartment_closed_without_its_carrier_collects_it_on_every_carrier(recipe):
    """Documented: a scope that does not name the carrier matches all carriers."""
    with Engine() as e:
        e.register("p", recipe("compartment"))
        e.submit("p", {"value": 1}, scope={"carrier": 1, "compartment": 3})
        e.submit("p", {"value": 10}, scope={"carrier": 2, "compartment": 3})
        e.submit("p", {"value": 100}, scope={"carrier": 2, "compartment": 4})
        e.submit("p", {"value": 0}, scope={"compartment": 3}, complete="compartment")
        results = scoped(e, 1)

    (compartment,) = [r for r in results if r.get("level") == "compartment"]
    assert compartment["total"] == 11


# -- Scopes per step ---------------------------------------------------


@pytest.mark.parametrize("summary_level, comparison_level, expected_summaries", [
    ("compartment", "carrier", 2),
    ("group", "compartment", 4),
    ("group", "carrier", 4),
])
def test_the_same_tiles_summarised_at_another_level(recipe, summary_level, comparison_level,
                                                    expected_summaries):
    """Moving a step to another scope changes only how the tiles are grouped."""
    with Engine() as e:
        e.register("p", recipe(summary_level, comparison_level))
        for compartment in (1, 2):
            for group in (1, 2):
                closes = [level for level, closed in [
                    ("group", True),
                    ("compartment", group == 2),
                    ("carrier", group == 2 and compartment == 2),
                ] if level in (summary_level, comparison_level) and closed]
                submit_tiles(e, [compartment * 10 + group] * 2,
                             {"carrier": 1, "compartment": compartment, "group": group},
                             complete=closes)
        results = scoped(e, expected_summaries + (2 if comparison_level == "compartment" else 1))

    summaries = by_unit(results, summary_level)
    comparisons = by_unit(results, comparison_level)
    assert len(summaries) == expected_summaries
    assert sum(r["total"] for r in comparisons.values()) == 2 * (11 + 12 + 21 + 22)


def test_several_steps_share_one_scope(recipe):
    """A step with no scope after a scoped one runs in the same phase."""
    with Engine() as e:
        e.register("p", recipe("compartment", then_double=True))
        submit_tiles(e, [1, 2, 3], {"compartment": 1}, complete="compartment")
        results = scoped(e, 1)

    (compartment,) = [r for r in results if r["_phase"] == 1]
    assert compartment["total"] == 6
    assert compartment["doubled"] == 12
    assert set(compartment["provenance"]) == {"unit", "double"}


# -- Levels the recipe declares ----------------------------------------


SAMPLE = ["carrier", "compartment", "group"]


def interleaved_groups(e, complete):
    """Group 1 of compartments 1 and 2, tiles mixed, then both closed."""
    for value in (1, 100):
        for _ in range(2):
            e.submit("p", {"value": value},
                     scope={"carrier": 1, "compartment": 1 if value == 1 else 2, "group": 1})
    e.submit("p", {"value": 0}, scope={"carrier": 1, "compartment": 1, "group": 1}, complete=complete)
    e.submit("p", {"value": 0}, scope={"carrier": 1, "compartment": 2, "group": 1}, complete=complete)


def test_a_recipe_that_skips_a_level_keeps_units_apart_when_it_declares_its_levels(recipe):
    with Engine() as e:
        e.register("p", recipe("group", "carrier", levels=SAMPLE))
        interleaved_groups(e, complete="group")
        results = scoped(e, 2)

    groups = {r["engine_unit"]["compartment"]: r for r in results if r["_phase"] == 1}
    assert groups[1]["total"] == 2 and groups[2]["total"] == 200
    assert list(groups[1]["engine_unit"]) == ["carrier", "compartment", "group"]


def test_without_declared_levels_a_skipped_level_is_not_part_of_the_unit(recipe):
    """Documented: the unit is then the closed level plus the wider phases."""
    with Engine() as e:
        e.register("p", recipe("group", "carrier"))
        interleaved_groups(e, complete="group")
        results = scoped(e, 2)

    totals = sorted(r["total"] for r in results if r["_phase"] == 1)
    assert totals == [0, 202]


def test_the_unit_is_widest_first_and_empty_for_all(recipe):
    with Engine() as e:
        e.register("p", recipe("compartment", "all", levels=SAMPLE))
        submit_tiles(e, [1], {"carrier": 2, "compartment": 3, "group": 4},
                     complete=["compartment", "all"])
        results = scoped(e, 2)

    units = {r["level"]: r["engine_unit"] for r in results if r["_phase"] > 0}
    assert units["compartment"] == {"carrier": 2, "compartment": 3}
    assert list(units["compartment"]) == ["carrier", "compartment"]
    assert units["all"] == {}


def test_bad_levels_are_refused_at_register(recipe):
    with Engine() as e:
        with pytest.raises(ValueError, match="levels"):
            e.register("p", recipe("compartment", levels=["carrier", "carrier"]))


# -- What a narrower level never closed --------------------------------


def test_a_carrier_reports_a_compartment_not_closed_and_keeps_its_tiles(recipe):
    with Engine() as e:
        e.register("p", recipe("compartment", "carrier"))
        submit_tiles(e, [1, 2], {"carrier": 1, "compartment": 1}, complete="compartment")
        submit_tiles(e, [10, 20], {"carrier": 1, "compartment": 2})      # not closed
        submit_tiles(e, [3], {"carrier": 1, "compartment": 1}, complete=["compartment", "carrier"])
        results = scoped(e, 3)
        held = e.status("p")["held"]
        assert held == {"results": 2, "units": [{"carrier": 1, "compartment": 2}]}

        e.submit("p", {"value": 30}, scope={"carrier": 1, "compartment": 2}, complete="compartment")
        results += scoped(e, 1)
        # The late compartment's result now waits for carrier 1 to close again.
        assert e.status("p")["held"] == {"results": 1, "units": [{"carrier": 1}]}

    carrier = by_unit(results, "carrier")[(("carrier", 1),)]
    assert carrier["n_inputs"] == 2                 # compartment 1, closed twice
    assert carrier["n_failures"] == 2               # compartment 2's two tiles

    late = by_unit(results, "compartment")[(("carrier", 1), ("compartment", 2))]
    assert late["total"] == 60


def test_a_held_failure_is_reported_to_the_carrier_too(recipe):
    with Engine() as e:
        e.register("p", recipe("compartment", "carrier"))
        e.submit("p", {"fail": True}, scope={"carrier": 1, "compartment": 2})
        submit_tiles(e, [1], {"carrier": 1, "compartment": 1}, complete=["compartment", "carrier"])
        results = scoped(e, 2)

    carrier = by_unit(results, "carrier")[(("carrier", 1),)]
    assert carrier["n_failures"] == 1


# -- Signals out of the ordinary ---------------------------------------


def test_closing_a_level_the_recipe_does_not_have_runs_nothing(recipe):
    with Engine() as e:
        e.register("p", recipe("compartment"))
        submit_tiles(e, [1, 2], {"compartment": 1}, complete="carrier")
        submit_tiles(e, [3], {"compartment": 1}, complete="compartment")
        results = scoped(e, 1)

    (compartment,) = [r for r in results if r["_phase"] == 1]
    assert compartment["total"] == 6


def test_a_unit_closed_again_runs_again_on_its_new_tiles(recipe):
    with Engine() as e:
        e.register("p", recipe("compartment"))
        submit_tiles(e, [1, 2], {"compartment": 1}, complete="compartment")
        results = scoped(e, 1)
        e.submit("p", {"value": 5}, scope={"compartment": 2}, complete="compartment")
        e.submit("p", {"value": 0}, scope={"compartment": 1}, complete="compartment")
        results += scoped(e, 2)

    totals = sorted((r["unit"]["compartment"], r["total"]) for r in results if r["_phase"] == 1)
    assert totals == [(1, 0), (1, 3), (2, 5)]


def test_a_carrier_signalled_before_its_last_compartment_does_not_wait_for_it(recipe, steps_dir, tmp_path):
    """Compartment 2's close is sent after the carrier's, while the carrier
    is still waiting on a slow tile. The carrier must not wait for a signal
    queued behind it, or one engine thread is enough to hang the engine."""
    gate = tmp_path / "gate"
    (steps_dir / "slow_tile.py").write_text(textwrap.dedent(f"""
        import os, time
        def run(pd, state, **p):
            while not os.path.exists({str(gate)!r}):
                time.sleep(0.02)
            pd["value"] = pd["input"]["value"]
            return pd
    """))
    yaml = steps_dir / "recipe_slow.yaml"
    yaml.write_text(textwrap.dedent(f"""
        metadata:
          functions_dir: "{steps_dir.as_posix()}"
        wf:
          - slow_tile:
          - unit:
              scope: compartment
          - unit:
              scope: carrier
    """))
    with Engine(max_concurrent=1) as e:
        e.register("p", str(yaml))
        submit_tiles(e, [1], {"carrier": 1, "compartment": 1}, complete="compartment")
        e.submit("p", {"value": 0}, scope={"carrier": 1}, complete="carrier")
        submit_tiles(e, [10], {"carrier": 1, "compartment": 2}, complete="compartment")
        gate.write_text("go")
        results = scoped(e, 3, timeout=60)

    carrier = by_unit(results, "carrier")[(("carrier", 1),)]
    assert carrier["n_inputs"] == 1 and carrier["total"] == 1
    compartments = by_unit(results, "compartment")
    assert compartments[(("carrier", 1), ("compartment", 2))]["total"] == 10


def test_a_shutdown_now_releases_a_waiting_carrier(recipe, steps_dir, tmp_path):
    """The carrier waits on a tile that never finishes; shutdown(wait=False)
    cancels the queued compartment close and must still return."""
    (steps_dir / "stuck_tile.py").write_text(textwrap.dedent("""
        import time
        def run(pd, state, **p):
            time.sleep(60)
            return pd
    """))
    yaml = steps_dir / "recipe_stuck.yaml"
    yaml.write_text(textwrap.dedent(f"""
        metadata:
          functions_dir: "{steps_dir.as_posix()}"
        wf:
          - stuck_tile:
          - unit:
              scope: compartment
          - unit:
              scope: carrier
    """))
    e = Engine(max_concurrent=1)
    e.register("p", str(yaml))
    e.submit("p", {"value": 1}, scope={"carrier": 1, "compartment": 1}, complete="compartment")
    e.submit("p", {"value": 2}, scope={"carrier": 1, "compartment": 2},
             complete=["compartment", "carrier"])
    time.sleep(0.5)
    t0 = time.monotonic()
    e.shutdown(wait=False)
    assert time.monotonic() - t0 < 20


def test_many_units_with_few_threads_do_not_deadlock(recipe):
    """More open carriers than engine threads, each waiting on its compartments."""
    with Engine(max_concurrent=2) as e:
        e.register("p", recipe("compartment", "carrier"))
        for carrier in range(1, 7):
            for compartment in (1, 2):
                closes = ["compartment", "carrier"] if compartment == 2 else "compartment"
                submit_tiles(e, [carrier] * 2, {"carrier": carrier, "compartment": compartment},
                             complete=closes)
        results = scoped(e, 12 + 6, timeout=60)

    carriers = by_unit(results, "carrier")
    assert {key[0][1]: r["total"] for key, r in carriers.items()} == {c: 4 * c for c in range(1, 7)}


# -- Failures ----------------------------------------------------------


def test_a_failed_tile_reaches_its_compartment_and_no_other(recipe):
    with Engine() as e:
        e.register("p", recipe("compartment"))
        e.submit("p", {"fail": True}, scope={"compartment": 1})
        e.submit("p", {"fail": True}, scope={"compartment": 2})
        submit_tiles(e, [1, 2], {"compartment": 1}, complete="compartment")
        results = scoped(e, 1)
        status = status_when(e, lambda s: len(s["failures"]) == 1)

    (compartment,) = [r for r in results if r["_phase"] == 1]
    assert compartment["n_inputs"] == 2
    assert compartment["n_failures"] == 1
    assert len(status["failures"]) == 1      # compartment 2's, still unclaimed


def test_a_compartment_of_failed_tiles_still_runs(recipe):
    """The step is told its tiles failed, rather than being skipped."""
    with Engine() as e:
        e.register("p", recipe("compartment"))
        e.submit("p", {"fail": True}, scope={"compartment": 1}, complete="compartment")
        results = scoped(e, 1)

    (compartment,) = [r for r in results if r["_phase"] == 1]
    assert compartment["n_inputs"] == 0
    assert compartment["n_failures"] == 1


def test_a_failed_compartment_reaches_its_carrier(recipe):
    with Engine() as e:
        e.register("p", recipe("compartment", "carrier"))
        submit_tiles(e, [1], {"carrier": 1, "compartment": 1}, complete="compartment")
        submit_tiles(e, [666], {"carrier": 1, "compartment": 2},
                     complete=["compartment", "carrier"])
        results = scoped(e, 2)
        status = e.status("p")

    carrier = by_unit(results, "carrier")[(("carrier", 1),)]
    assert carrier["n_inputs"] == 1
    assert carrier["n_failures"] == 1
    assert status["failures"] == []           # claimed by the carrier


def test_a_carrier_of_failed_compartments_still_runs(recipe):
    with Engine() as e:
        e.register("p", recipe("compartment", "carrier"))
        submit_tiles(e, [666], {"carrier": 1, "compartment": 1},
                     complete=["compartment", "carrier"])
        results = scoped(e, 1)

    carrier = by_unit(results, "carrier")[(("carrier", 1),)]
    assert carrier["n_inputs"] == 0
    assert carrier["n_failures"] == 1


def test_a_failure_names_its_step_phase_and_tile(recipe):
    with Engine() as e:
        e.register("p", recipe("compartment"))
        e.submit("p", {"fail": True}, scope={"compartment": 1})        # idx 0
        e.submit("p", {"value": 666}, scope={"compartment": 2}, complete="compartment")  # idx 1
        status = status_when(e, lambda s: len(s["failures"]) == 2)

    by_step = {f["step"]: f for f in status["failures"]}
    assert by_step["tile"]["phase"] == 0 and by_step["tile"]["submission_idx"] == 0
    assert by_step["unit"]["phase"] == 1 and by_step["unit"]["submission_idx"] == 1
    assert by_step["unit"]["scope"] == {"compartment": 2}


def test_a_failed_compartment_step_is_not_handed_to_the_next_close_as_a_tile(recipe):
    with Engine() as e:
        e.register("p", recipe("compartment"))
        submit_tiles(e, [666], {"compartment": 1}, complete="compartment")
        status_when(e, lambda s: len(s["failures"]) == 1)
        submit_tiles(e, [1], {"compartment": 1}, complete="compartment")
        results = scoped(e, 1)
        status = e.status("p")

    (compartment,) = [r for r in results if r["_phase"] == 1]
    assert compartment["n_failures"] == 0
    assert [f["step"] for f in status["failures"]] == ["unit"]


def test_all_gets_only_the_failures_of_the_phase_before_it(recipe):
    with Engine() as e:
        e.register("p", recipe("compartment", "all"))
        e.submit("p", {"fail": True}, scope={"compartment": 1})
        submit_tiles(e, [1], {"compartment": 1}, complete="compartment")
        submit_tiles(e, [666], {"compartment": 2}, complete=["compartment", "all"])
        results = scoped(e, 2)

    (everything,) = [r for r in results if r.get("level") == "all"]
    assert everything["n_inputs"] == 1
    assert everything["n_failures"] == 1     # compartment 2's step, not the tile


# -- Provenance --------------------------------------------------------


def test_every_level_records_its_own_step_and_scope(recipe):
    with Engine() as e:
        e.register("p", recipe("compartment", "carrier"))
        submit_tiles(e, [1, 2], {"carrier": 1, "compartment": 1},
                     complete=["compartment", "carrier"])
        results = scoped(e, 2)

    for r in results:
        assert set(r["_scope"]) == {"carrier", "compartment"}
        if r["_phase"] == 0:
            assert set(r["provenance"]) == {"tile"}
            assert r["_scope_level"] is None
        else:
            assert set(r["provenance"]) == {"unit"}
            assert r["_scope_level"] == r["level"]
    compartment = by_unit(results, "compartment")[(("carrier", 1), ("compartment", 1))]
    assert compartment["inputs_provenance"] == [["tile"], ["tile"]]


@pytest.mark.xfail(strict=True, reason="known gap: a scoped result does not say which tiles it "
                                       "was built from, nor where their steps ran, unless the "
                                       "step copies that over itself")
def test_a_carrier_can_be_traced_back_to_its_tiles(recipe):
    with Engine() as e:
        e.register("p", recipe("compartment", "carrier"))
        submit_tiles(e, [1, 2], {"carrier": 1, "compartment": 1},
                     complete=["compartment", "carrier"])
        results = scoped(e, 2)

    carrier = by_unit(results, "carrier")[(("carrier", 1),)]
    assert "tile" in carrier["provenance"]
