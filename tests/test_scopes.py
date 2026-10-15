"""
Analysis over scopes, end to end.

A sample is divided into levels, narrowest first: tile, group, compartment,
carrier. Each test runs a real pipeline through the engine and checks what
every level receives: which tiles, which failures, which provenance.

The steps are deliberately small. ``tile`` passes a number on; ``unit``
adds up whatever the level below it handed in. So every expected value
can be worked out by hand.

The class at the end holds the engine's older scope tests, one case each,
written against the small step and pipeline helpers of ``helpers.py``.

Usage
-----
    python -m pytest tests/test_scopes.py -v
"""

import textwrap
import time

import pytest
from helpers import (
    ENGINE_METADATA,
    _wait_for_results,
    _wait_for_status,
    failures,
    fresh_output,
    outputs,
    phase_of,
    tally,
)
from helpers import _write_step as _temp_step
from helpers import _write_yaml as _temp_yaml

from zmart_analysis import Engine, ScopeError

pytestmark = pytest.mark.integration

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
        (folder / f"{name}.py").write_text(textwrap.dedent(code) + ENGINE_METADATA)
    return folder


@pytest.fixture
def pipeline(steps_dir):
    """pipeline("compartment", "carrier") -> tile, then one unit step per scope."""

    def build(*scopes, then_double=False, levels=None):
        lines = [f'metadata:\n  functions_dir: "{steps_dir.as_posix()}"']
        if levels:
            lines.append(f"  levels: [{', '.join(levels)}]")
        lines += ["wf:", "  - tile:"]
        for scope in scopes:
            lines += ["  - unit:", f"      scope: {scope}"]
        if then_double:
            lines.append("  - double:")
        path = steps_dir / f"pipeline_{'_'.join(scopes)}_{then_double}_{bool(levels)}.yaml"
        path.write_text("\n".join(lines) + "\n")
        return str(path)

    return build


def scoped(engine, expected, timeout=30):
    """Poll the board until *expected* scoped jobs are done; return the
    outputs of every done job, tiles and units, in job order.

    The board keeps every output, so each call returns all of them again: a
    test that closes more units later asks for the new running total.
    """
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        done = outputs(engine.status("p"))
        if sum(phase_of(r) > 0 for r in done) >= expected:
            return done
        time.sleep(0.05)
    board = engine.status("p")
    raise TimeoutError(
        f"{expected} scoped results expected within {timeout}s, got "
        f"{sum(phase_of(r) > 0 for r in outputs(board))}; board {board}"
    )


def status_when(engine, condition, timeout=30):
    """Poll the board until *condition* holds; return the board."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        board = engine.status("p")
        if condition(board):
            return board
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


def test_three_levels_each_add_up_their_own_units(pipeline):
    """tile -> group -> compartment -> carrier, two of each."""
    with Engine() as e:
        e.register("p", pipeline("group", "compartment", "carrier"), fresh_output())
        for carrier in (1, 2):
            for compartment in (1, 2):
                for group in (1, 2):
                    closes = ["group"]
                    if group == 2:
                        closes.append("compartment")
                    if group == 2 and compartment == 2:
                        closes.append("carrier")
                    value = 1000 * carrier + 100 * compartment + 10 * group
                    submit_tiles(
                        e,
                        [value, value + 1],
                        {"carrier": carrier, "compartment": compartment, "group": group},
                        complete=closes,
                    )
        results = scoped(e, 8 + 4 + 2)

    groups = by_unit(results, "group")
    compartments = by_unit(results, "compartment")
    carriers = by_unit(results, "carrier")
    assert len(groups) == 8 and len(compartments) == 4 and len(carriers) == 2

    assert groups[(("carrier", 2), ("compartment", 1), ("group", 2))]["total"] == 2120 + 2121
    assert compartments[(("carrier", 1), ("compartment", 2))]["total"] == 1210 + 1211 + 1220 + 1221
    assert compartments[(("carrier", 1), ("compartment", 2))]["n_inputs"] == 2
    for c in (1, 2):
        every_tile = [
            1000 * c + 100 * m + 10 * g + d for m in (1, 2) for g in (1, 2) for d in (0, 1)
        ]
        assert carriers[(("carrier", c),)]["total"] == sum(every_tile)
    assert carriers[(("carrier", 2),)]["n_inputs"] == 2


def test_two_carriers_interleaved_keep_their_compartments_apart(pipeline):
    """Both carriers have a compartment 1; tiles of the two arrive mixed."""
    with Engine(max_concurrent=4) as e:
        e.register("p", pipeline("compartment", "carrier"), fresh_output())
        for _ in range(3):
            e.submit("p", {"value": 1}, scope={"carrier": 1, "compartment": 1})
            e.submit("p", {"value": 100}, scope={"carrier": 2, "compartment": 1})
        e.submit(
            "p",
            {"value": 0},
            scope={"carrier": 2, "compartment": 1},
            complete=["compartment", "carrier"],
        )
        e.submit(
            "p",
            {"value": 0},
            scope={"carrier": 1, "compartment": 1},
            complete=["compartment", "carrier"],
        )
        results = scoped(e, 4)

    carriers = by_unit(results, "carrier")
    assert carriers[(("carrier", 1),)]["total"] == 3
    assert carriers[(("carrier", 2),)]["total"] == 300


def test_a_wider_level_closed_by_a_later_submit_waits_for_the_narrower(pipeline):
    """The carrier is closed by its own submit, after slow compartments."""
    with Engine() as e:
        e.register("p", pipeline("compartment", "carrier"), fresh_output())
        for compartment in (1, 2, 3):
            submit_tiles(
                e,
                [compartment] * 4,
                {"carrier": 1, "compartment": compartment},
                complete="compartment",
            )
        e.submit("p", {"value": 0}, scope={"carrier": 1}, complete="carrier")
        results = scoped(e, 4)

    carrier = by_unit(results, "carrier")[(("carrier", 1),)]
    assert carrier["n_inputs"] == 3
    assert carrier["total"] == 4 * (1 + 2 + 3)


def test_all_after_carriers_collects_every_carrier(pipeline):
    with Engine() as e:
        e.register("p", pipeline("compartment", "carrier", "all"), fresh_output())
        submit_tiles(
            e, [1, 2], {"carrier": 1, "compartment": 1}, complete=["compartment", "carrier"]
        )
        submit_tiles(
            e,
            [10, 20],
            {"carrier": 2, "compartment": 1},
            complete=["compartment", "carrier", "all"],
        )
        results = scoped(e, 5)

    everything = [r for r in results if r.get("level") == "all"]
    assert len(everything) == 1
    assert everything[0]["total"] == 33
    assert everything[0]["n_inputs"] == 2


def test_a_compartment_closed_without_its_carrier_collects_it_on_every_carrier(pipeline):
    """Documented: a scope that does not name the carrier matches all carriers."""
    with Engine() as e:
        e.register("p", pipeline("compartment"), fresh_output())
        e.submit("p", {"value": 1}, scope={"carrier": 1, "compartment": 3})
        e.submit("p", {"value": 10}, scope={"carrier": 2, "compartment": 3})
        e.submit("p", {"value": 100}, scope={"carrier": 2, "compartment": 4})
        e.submit("p", {"value": 0}, scope={"compartment": 3}, complete="compartment")
        results = scoped(e, 1)

    (compartment,) = [r for r in results if r.get("level") == "compartment"]
    assert compartment["total"] == 11


# -- Scopes per step ---------------------------------------------------


@pytest.mark.parametrize(
    "summary_level, comparison_level, expected_summaries",
    [
        ("compartment", "carrier", 2),
        ("group", "compartment", 4),
        ("group", "carrier", 4),
    ],
)
def test_the_same_tiles_summarised_at_another_level(
    pipeline, summary_level, comparison_level, expected_summaries
):
    """Moving a step to another scope changes only how the tiles are grouped."""
    with Engine() as e:
        e.register("p", pipeline(summary_level, comparison_level), fresh_output())
        for compartment in (1, 2):
            for group in (1, 2):
                closes = [
                    level
                    for level, closed in [
                        ("group", True),
                        ("compartment", group == 2),
                        ("carrier", group == 2 and compartment == 2),
                    ]
                    if level in (summary_level, comparison_level) and closed
                ]
                submit_tiles(
                    e,
                    [compartment * 10 + group] * 2,
                    {"carrier": 1, "compartment": compartment, "group": group},
                    complete=closes,
                )
        results = scoped(e, expected_summaries + (2 if comparison_level == "compartment" else 1))

    summaries = by_unit(results, summary_level)
    comparisons = by_unit(results, comparison_level)
    assert len(summaries) == expected_summaries
    assert sum(r["total"] for r in comparisons.values()) == 2 * (11 + 12 + 21 + 22)


def test_several_steps_share_one_scope(pipeline):
    """A step with no scope after a scoped one runs in the same phase."""
    with Engine() as e:
        e.register("p", pipeline("compartment", then_double=True), fresh_output())
        submit_tiles(e, [1, 2, 3], {"compartment": 1}, complete="compartment")
        results = scoped(e, 1)

    (compartment,) = [r for r in results if phase_of(r) == 1]
    assert compartment["total"] == 6
    assert compartment["doubled"] == 12
    assert set(compartment["provenance"]) == {"unit", "double"}


# -- Levels the pipeline declares ----------------------------------------


SAMPLE = ["carrier", "compartment", "group"]


def interleaved_groups(e, complete):
    """Group 1 of compartments 1 and 2, tiles mixed, then both closed."""
    for value in (1, 100):
        for _ in range(2):
            e.submit(
                "p",
                {"value": value},
                scope={"carrier": 1, "compartment": 1 if value == 1 else 2, "group": 1},
            )
    e.submit(
        "p", {"value": 0}, scope={"carrier": 1, "compartment": 1, "group": 1}, complete=complete
    )
    e.submit(
        "p", {"value": 0}, scope={"carrier": 1, "compartment": 2, "group": 1}, complete=complete
    )


def test_a_pipeline_that_skips_a_level_keeps_units_apart_when_it_declares_its_levels(pipeline):
    with Engine() as e:
        e.register("p", pipeline("group", "carrier", levels=SAMPLE), fresh_output())
        interleaved_groups(e, complete="group")
        results = scoped(e, 2)

    groups = {r["engine_unit"]["compartment"]: r for r in results if phase_of(r) == 1}
    assert groups[1]["total"] == 2 and groups[2]["total"] == 200
    assert list(groups[1]["engine_unit"]) == ["carrier", "compartment", "group"]


def test_without_declared_levels_a_skipped_level_is_not_part_of_the_unit(pipeline):
    """Documented: the unit is then the closed level plus the wider phases."""
    with Engine() as e:
        e.register("p", pipeline("group", "carrier"), fresh_output())
        interleaved_groups(e, complete="group")
        results = scoped(e, 2)

    totals = sorted(r["total"] for r in results if phase_of(r) == 1)
    assert totals == [0, 202]


def test_the_unit_is_widest_first_and_empty_for_all(pipeline):
    with Engine() as e:
        e.register("p", pipeline("compartment", "all", levels=SAMPLE), fresh_output())
        submit_tiles(
            e, [1], {"carrier": 2, "compartment": 3, "group": 4}, complete=["compartment", "all"]
        )
        results = scoped(e, 2)

    units = {r["level"]: r["engine_unit"] for r in results if phase_of(r) > 0}
    assert units["compartment"] == {"carrier": 2, "compartment": 3}
    assert list(units["compartment"]) == ["carrier", "compartment"]
    assert units["all"] == {}


def test_bad_levels_are_refused_at_register(pipeline):
    with Engine() as e:
        with pytest.raises(ValueError, match="levels"):
            e.register("p", pipeline("compartment", levels=["carrier", "carrier"]), fresh_output())


# -- A wider level completes the narrower ones -------------------------
#
# Once a carrier is complete nothing more can arrive inside it, so its
# compartments and groups are complete too. Completing a level first closes
# every narrower unit of it that is still open, narrowest first. The
# experiment is the widest level: completing it closes everything.

EXPERIMENT = ["experiment", "carrier", "compartment", "group"]


def scope_jobs(board):
    """The scope jobs of a board, in job order, as (step level, scope)."""
    return [
        (entry["output"]["level"], entry["scope"])
        for job, entry in sorted(board.items())
        if entry["state"] == "done" and phase_of(entry["output"]) > 0
    ]


def test_completing_a_carrier_completes_its_open_compartments_and_groups(pipeline):
    with Engine() as e:
        e.register(
            "p", pipeline("group", "compartment", "carrier", levels=EXPERIMENT), fresh_output()
        )
        for compartment in (1, 2):
            for group in (1, 2):
                value = 100 * compartment + 10 * group
                submit_tiles(
                    e,
                    [value, value + 1],
                    {"carrier": 1, "compartment": compartment, "group": group},
                )
        e.submit(
            "p",
            {"value": 0},
            scope={"carrier": 1, "compartment": 2, "group": 2},
            complete="carrier",
        )
        results = scoped(e, 4 + 2 + 1)
        board = e.status("p")

    assert scope_jobs(board) == [
        ("group", {"carrier": 1, "compartment": 1, "group": 1}),
        ("group", {"carrier": 1, "compartment": 1, "group": 2}),
        ("group", {"carrier": 1, "compartment": 2, "group": 1}),
        ("group", {"carrier": 1, "compartment": 2, "group": 2}),
        ("compartment", {"carrier": 1, "compartment": 1}),
        ("compartment", {"carrier": 1, "compartment": 2}),
        ("carrier", {"carrier": 1}),
    ]
    groups = by_unit(results, "group")
    assert groups[(("carrier", 1), ("compartment", 2), ("group", 2))]["total"] == 220 + 221 + 0
    carrier = by_unit(results, "carrier")[(("carrier", 1),)]
    every_tile = [100 * m + 10 * g + d for m in (1, 2) for g in (1, 2) for d in (0, 1)]
    assert carrier["total"] == sum(every_tile)
    assert carrier["n_inputs"] == 2
    assert carrier["n_failures"] == 0


def test_completing_a_carrier_leaves_the_other_carriers_open(pipeline):
    with Engine() as e:
        e.register("p", pipeline("compartment", "carrier", levels=EXPERIMENT), fresh_output())
        submit_tiles(e, [1, 2], {"carrier": 1, "compartment": 1})
        submit_tiles(e, [10], {"carrier": 2, "compartment": 1})
        e.submit("p", {"value": 3}, scope={"carrier": 1, "compartment": 2}, complete="carrier")
        scoped(e, 3)
        board = e.status("p")

    assert scope_jobs(board) == [
        ("compartment", {"carrier": 1, "compartment": 1}),
        ("compartment", {"carrier": 1, "compartment": 2}),
        ("carrier", {"carrier": 1}),
    ]


def test_completing_the_experiment_completes_every_carrier_and_what_is_in_it(pipeline):
    with Engine() as e:
        e.register(
            "p", pipeline("compartment", "carrier", "experiment", levels=EXPERIMENT), fresh_output()
        )
        submit_tiles(e, [1, 2], {"carrier": 1, "compartment": 1})
        submit_tiles(e, [10], {"carrier": 1, "compartment": 2})
        submit_tiles(e, [100], {"carrier": 2, "compartment": 1})
        e.submit(
            "p", {"value": 1000}, scope={"carrier": 2, "compartment": 2}, complete="experiment"
        )
        results = scoped(e, 4 + 2 + 1)
        board = e.status("p")

    assert [level for level, _ in scope_jobs(board)] == (
        ["compartment"] * 4 + ["carrier"] * 2 + ["experiment"]
    )
    (experiment,) = [r for r in results if r.get("level") == "experiment"]
    assert experiment["total"] == 1 + 2 + 10 + 100 + 1000
    assert experiment["n_inputs"] == 2


def test_a_unit_already_completed_is_not_completed_again(pipeline):
    with Engine() as e:
        e.register("p", pipeline("compartment", "carrier", levels=EXPERIMENT), fresh_output())
        submit_tiles(e, [1, 2], {"carrier": 1, "compartment": 1}, complete="compartment")
        submit_tiles(e, [10], {"carrier": 1, "compartment": 2})
        e.submit("p", {"value": 20}, scope={"carrier": 1, "compartment": 2}, complete="carrier")
        results = scoped(e, 3)
        board = e.status("p")

    assert scope_jobs(board) == [
        ("compartment", {"carrier": 1, "compartment": 1}),
        ("compartment", {"carrier": 1, "compartment": 2}),
        ("carrier", {"carrier": 1}),
    ]
    assert by_unit(results, "carrier")[(("carrier", 1),)]["total"] == 33


def test_a_failed_tile_of_a_compartment_left_open_reaches_that_compartment(pipeline):
    with Engine() as e:
        e.register("p", pipeline("compartment", "carrier"), fresh_output())
        e.submit("p", {"fail": True}, scope={"carrier": 1, "compartment": 2})
        submit_tiles(e, [1], {"carrier": 1, "compartment": 1}, complete="carrier")
        results = scoped(e, 3)

    compartments = by_unit(results, "compartment")
    assert compartments[(("carrier", 1), ("compartment", 2))]["n_failures"] == 1
    assert compartments[(("carrier", 1), ("compartment", 1))]["total"] == 1
    carrier = by_unit(results, "carrier")[(("carrier", 1),)]
    assert carrier["n_inputs"] == 2 and carrier["n_failures"] == 0


def test_a_tile_after_its_compartment_closed_opens_it_again(pipeline):
    """A late tile opens its compartment again, so completing the carrier
    closes that compartment a second time, on the late tile alone."""
    with Engine() as e:
        e.register("p", pipeline("compartment", "carrier"), fresh_output())
        submit_tiles(e, [1, 2], {"carrier": 1, "compartment": 1}, complete="compartment")
        scoped(e, 1)
        e.submit("p", {"value": 10}, scope={"carrier": 1, "compartment": 1})
        e.submit("p", {"value": 0}, scope={"carrier": 1, "compartment": 2}, complete="carrier")
        results = scoped(e, 4)
        board = e.status("p")

    assert scope_jobs(board) == [
        ("compartment", {"carrier": 1, "compartment": 1}),
        ("compartment", {"carrier": 1, "compartment": 1}),
        ("compartment", {"carrier": 1, "compartment": 2}),
        ("carrier", {"carrier": 1}),
    ]
    totals = [r["total"] for r in results if r.get("level") == "compartment"]
    assert totals == [3, 10, 0]
    carrier = by_unit(results, "carrier")[(("carrier", 1),)]
    assert carrier["n_inputs"] == 3 and carrier["total"] == 13 and carrier["n_failures"] == 0


# -- Signals out of the ordinary ---------------------------------------


def test_closing_a_level_the_pipeline_does_not_have_runs_nothing(pipeline):
    with Engine() as e:
        e.register("p", pipeline("compartment"), fresh_output())
        submit_tiles(e, [1, 2], {"compartment": 1}, complete="carrier")
        submit_tiles(e, [3], {"compartment": 1}, complete="compartment")
        results = scoped(e, 1)
        board = e.status("p")

    (compartment,) = [r for r in results if phase_of(r) == 1]
    assert compartment["total"] == 6
    # Three tiles and one scope job: the carrier close made none.
    assert len(board) == 4


def test_a_close_whose_scope_leaves_out_the_level_is_refused(pipeline):
    """{"carrier": 1} with complete="compartment" would close every
    compartment of every carrier. Refused, and the tile is not run."""
    with Engine() as e:
        e.register("p", pipeline("compartment", "carrier"), fresh_output())
        submit_tiles(e, [1], {"carrier": 1, "compartment": 1}, complete="compartment")
        with pytest.raises(ScopeError, match="compartment"):
            e.submit("p", {"value": 5}, scope={"carrier": 1}, complete="compartment")
        with pytest.raises(ScopeError):
            e.submit("p", {"value": 5}, scope={"carrier": 1}, complete=["compartment", "carrier"])
        results = scoped(e, 1)
        board = e.status("p")

    assert [r["value"] for r in results if phase_of(r) == 0] == [1]
    # The refused submits put no job on the board: the tile and its unit only.
    assert sorted(board) == [1, 2]
    assert tally(board)["done"] == 2 and tally(board)["failed"] == 0


def test_all_and_a_level_the_pipeline_lacks_are_still_accepted(pipeline):
    with Engine() as e:
        e.register("p", pipeline("compartment", "all"), fresh_output())
        submit_tiles(e, [1], {"carrier": 1, "compartment": 1}, complete="compartment")
        e.submit("p", {"value": 2}, scope={"carrier": 1}, complete=["carrier", "all"])
        results = scoped(e, 2)

    (everything,) = [r for r in results if r.get("level") == "all"]
    assert everything["n_inputs"] == 1


def test_a_unit_closed_again_runs_again_on_its_new_tiles(pipeline):
    with Engine() as e:
        e.register("p", pipeline("compartment"), fresh_output())
        submit_tiles(e, [1, 2], {"compartment": 1}, complete="compartment")
        scoped(e, 1)
        e.submit("p", {"value": 5}, scope={"compartment": 2}, complete="compartment")
        e.submit("p", {"value": 0}, scope={"compartment": 1}, complete="compartment")
        results = scoped(e, 3)

    totals = sorted((r["unit"]["compartment"], r["total"]) for r in results if phase_of(r) == 1)
    assert totals == [(1, 0), (1, 3), (2, 5)]


def test_a_carrier_signalled_before_its_last_compartment_does_not_wait_for_it(
    pipeline, steps_dir, tmp_path
):
    """Compartment 2's close is sent after the carrier's, while the carrier
    is still waiting on a slow tile. The carrier must not wait for a signal
    queued behind it, or one engine thread is enough to hang the engine."""
    gate = tmp_path / "gate"
    (steps_dir / "slow_tile.py").write_text(
        textwrap.dedent(f"""
        import os, time
        def run(pd, state, **p):
            while not os.path.exists({str(gate)!r}):
                time.sleep(0.02)
            pd["value"] = pd["input"]["value"]
            return pd
    """)
        + ENGINE_METADATA
    )
    yaml = steps_dir / "pipeline_slow.yaml"
    yaml.write_text(
        textwrap.dedent(f"""
        metadata:
          functions_dir: "{steps_dir.as_posix()}"
        wf:
          - slow_tile:
          - unit:
              scope: compartment
          - unit:
              scope: carrier
    """)
    )
    with Engine(max_concurrent=1) as e:
        e.register("p", str(yaml), fresh_output())
        submit_tiles(e, [1], {"carrier": 1, "compartment": 1}, complete="compartment")
        e.submit("p", {"value": 0}, scope={"carrier": 1}, complete="carrier")
        submit_tiles(e, [10], {"carrier": 1, "compartment": 2}, complete="compartment")
        gate.write_text("go")
        results = scoped(e, 3, timeout=60)

    carrier = by_unit(results, "carrier")[(("carrier", 1),)]
    assert carrier["n_inputs"] == 1 and carrier["total"] == 1
    compartments = by_unit(results, "compartment")
    assert compartments[(("carrier", 1), ("compartment", 2))]["total"] == 10


def test_a_shutdown_now_releases_a_waiting_carrier(pipeline, steps_dir, tmp_path):
    """The carrier waits on a tile that never finishes; shutdown(wait=False)
    cancels the queued compartment close and must still return."""
    (steps_dir / "stuck_tile.py").write_text(
        textwrap.dedent("""
        import time
        def run(pd, state, **p):
            time.sleep(60)
            return pd
    """)
        + ENGINE_METADATA
    )
    yaml = steps_dir / "pipeline_stuck.yaml"
    yaml.write_text(
        textwrap.dedent(f"""
        metadata:
          functions_dir: "{steps_dir.as_posix()}"
        wf:
          - stuck_tile:
          - unit:
              scope: compartment
          - unit:
              scope: carrier
    """)
    )
    e = Engine(max_concurrent=1)
    e.register("p", str(yaml), fresh_output())
    e.submit("p", {"value": 1}, scope={"carrier": 1, "compartment": 1}, complete="compartment")
    e.submit(
        "p",
        {"value": 2},
        scope={"carrier": 1, "compartment": 2},
        complete=["compartment", "carrier"],
    )
    time.sleep(0.5)
    t0 = time.monotonic()
    e.shutdown(wait=False)
    assert time.monotonic() - t0 < 20


def test_many_units_with_few_threads_do_not_deadlock(pipeline):
    """More open carriers than engine threads, each waiting on its compartments."""
    with Engine(max_concurrent=2) as e:
        e.register("p", pipeline("compartment", "carrier"), fresh_output())
        for carrier in range(1, 7):
            for compartment in (1, 2):
                closes = ["compartment", "carrier"] if compartment == 2 else "compartment"
                submit_tiles(
                    e,
                    [carrier] * 2,
                    {"carrier": carrier, "compartment": compartment},
                    complete=closes,
                )
        results = scoped(e, 12 + 6, timeout=60)

    carriers = by_unit(results, "carrier")
    assert {key[0][1]: r["total"] for key, r in carriers.items()} == {c: 4 * c for c in range(1, 7)}


# -- Failures ----------------------------------------------------------


def test_a_failed_tile_reaches_its_compartment_and_no_other(pipeline):
    """Compartment 2's failed tile is not handed to compartment 1; it is
    still there for compartment 2 when that closes."""
    with Engine() as e:
        e.register("p", pipeline("compartment"), fresh_output())
        e.submit("p", {"fail": True}, scope={"compartment": 1})
        e.submit("p", {"fail": True}, scope={"compartment": 2})
        submit_tiles(e, [1, 2], {"compartment": 1}, complete="compartment")
        scoped(e, 1)
        e.submit("p", {"value": 5}, scope={"compartment": 2}, complete="compartment")
        results = scoped(e, 2)
        board = e.status("p")

    compartments = by_unit(results, "compartment")
    assert compartments[(("compartment", 1),)]["n_inputs"] == 2
    assert compartments[(("compartment", 1),)]["n_failures"] == 1
    assert compartments[(("compartment", 2),)]["n_inputs"] == 1
    assert compartments[(("compartment", 2),)]["n_failures"] == 1
    assert [(f["job"], f["step"], f["scope"]) for f in failures(board)] == [
        (1, "tile", {"compartment": 1}),
        (2, "tile", {"compartment": 2}),
    ]


def test_a_compartment_of_failed_tiles_still_runs(pipeline):
    """The step is told its tiles failed, rather than being skipped."""
    with Engine() as e:
        e.register("p", pipeline("compartment"), fresh_output())
        e.submit("p", {"fail": True}, scope={"compartment": 1}, complete="compartment")
        results = scoped(e, 1)

    (compartment,) = [r for r in results if phase_of(r) == 1]
    assert compartment["n_inputs"] == 0
    assert compartment["n_failures"] == 1


def test_a_failed_compartment_reaches_its_carrier(pipeline):
    with Engine() as e:
        e.register("p", pipeline("compartment", "carrier"), fresh_output())
        submit_tiles(e, [1], {"carrier": 1, "compartment": 1}, complete="compartment")
        submit_tiles(
            e, [666], {"carrier": 1, "compartment": 2}, complete=["compartment", "carrier"]
        )
        results = scoped(e, 2)
        board = e.status("p")

    carrier = by_unit(results, "carrier")[(("carrier", 1),)]
    assert carrier["n_inputs"] == 1
    assert carrier["n_failures"] == 1
    (failed,) = failures(board)
    assert failed["job"] == 4  # compartment 2's scope job
    assert failed["step"] == "unit"
    assert failed["scope"] == {"carrier": 1, "compartment": 2}
    assert "unit failed" in failed["error"]
    # The carrier was told: its lineage names the failed step and its close.
    assert [(f["step"], f["submission_idx"]) for f in carrier["lineage"]["failed"]] == [("unit", 3)]


def test_a_carrier_of_failed_compartments_still_runs(pipeline):
    with Engine() as e:
        e.register("p", pipeline("compartment", "carrier"), fresh_output())
        submit_tiles(
            e, [666], {"carrier": 1, "compartment": 1}, complete=["compartment", "carrier"]
        )
        results = scoped(e, 1)

    carrier = by_unit(results, "carrier")[(("carrier", 1),)]
    assert carrier["n_inputs"] == 0
    assert carrier["n_failures"] == 1


def test_a_failure_names_its_step_job_and_scope(pipeline):
    """On the board a failed tile is its own job; a failed unit step is the
    scope job of the unit it closed. Phase and submission are what the
    lineage records add, see test_lineage_lists_a_failed_tile_and_a_failed_compartment."""
    with Engine() as e:
        e.register("p", pipeline("compartment"), fresh_output())
        e.submit("p", {"fail": True}, scope={"compartment": 1})  # job 1
        e.submit(
            "p", {"value": 666}, scope={"compartment": 2}, complete="compartment"
        )  # job 2, scope job 3
        board = status_when(e, lambda b: len(failures(b)) == 2)

    by_step = {f["step"]: f for f in failures(board)}
    assert by_step["tile"]["job"] == 1 and by_step["tile"]["scope"] == {"compartment": 1}
    assert "tile failed" in by_step["tile"]["error"]
    assert by_step["unit"]["job"] == 3 and by_step["unit"]["scope"] == {"compartment": 2}
    assert "unit failed" in by_step["unit"]["error"]
    assert board[2]["state"] == "done"


def test_a_failed_compartment_step_is_not_handed_to_the_next_close_as_a_tile(pipeline):
    with Engine() as e:
        e.register("p", pipeline("compartment"), fresh_output())
        submit_tiles(e, [666], {"compartment": 1}, complete="compartment")
        status_when(e, lambda b: len(failures(b)) == 1)
        submit_tiles(e, [1], {"compartment": 1}, complete="compartment")
        results = scoped(e, 1)
        board = e.status("p")

    (compartment,) = [r for r in results if phase_of(r) == 1]
    assert compartment["n_inputs"] == 1
    assert compartment["n_failures"] == 0
    assert [(f["job"], f["step"]) for f in failures(board)] == [(2, "unit")]


def test_all_gets_only_the_failures_of_the_phase_before_it(pipeline):
    with Engine() as e:
        e.register("p", pipeline("compartment", "all"), fresh_output())
        e.submit("p", {"fail": True}, scope={"compartment": 1})
        submit_tiles(e, [1], {"compartment": 1}, complete="compartment")
        submit_tiles(e, [666], {"compartment": 2}, complete=["compartment", "all"])
        results = scoped(e, 2)

    (everything,) = [r for r in results if r.get("level") == "all"]
    assert everything["n_inputs"] == 1
    assert everything["n_failures"] == 1  # compartment 2's step, not the tile


# -- Provenance --------------------------------------------------------


def test_every_level_records_its_own_step_and_scope(pipeline):
    with Engine() as e:
        e.register("p", pipeline("compartment", "carrier"), fresh_output())
        submit_tiles(
            e, [1, 2], {"carrier": 1, "compartment": 1}, complete=["compartment", "carrier"]
        )
        results = scoped(e, 2)

    assert len(results) == 4
    for r in results:
        assert set(r["metadata"]["scope"]) == {"carrier", "compartment"}
        if phase_of(r) == 0:
            assert set(r["provenance"]) == {"tile"}
            assert r["metadata"].get("scope_level") is None
        else:
            assert set(r["provenance"]) == {"unit"}
            assert r["metadata"].get("scope_level") == r["level"]
    compartment = by_unit(results, "compartment")[(("carrier", 1), ("compartment", 1))]
    assert compartment["inputs_provenance"] == [["tile"], ["tile"]]


def test_a_carrier_can_be_traced_back_to_its_tiles(pipeline):
    """Lineage: which tiles a unit was built from, and where every step
    below it ran, through three levels."""
    with Engine() as e:
        e.register(
            "p", pipeline("group", "compartment", "carrier", then_double=True), fresh_output()
        )
        submit_tiles(e, [1, 2], {"carrier": 1, "compartment": 1, "group": 1}, complete="group")
        submit_tiles(
            e,
            [3],
            {"carrier": 1, "compartment": 1, "group": 2},
            complete=["group", "compartment", "carrier"],
        )
        results = scoped(e, 4)

    # The image jobs are 1, 2 and 4: job 3 is group 1's scope job.
    group = by_unit(results, "group")[(("carrier", 1), ("compartment", 1), ("group", 1))]
    assert group["lineage"]["submissions"] == [1, 2]
    assert set(group["lineage"]["provenance"]) == {"tile"}
    assert set(group["provenance"]) == {"unit"}

    carrier = by_unit(results, "carrier")[(("carrier", 1),)]
    assert carrier["lineage"]["submissions"] == [1, 2, 4]
    assert carrier["lineage"]["failed"] == []
    assert set(carrier["lineage"]["provenance"]) == {"tile", "unit"}  # below the carrier
    assert len(carrier["lineage"]["provenance"]["unit"]) == 1  # one environment
    assert set(carrier["provenance"]) == {"unit", "double"}


def test_a_scoped_result_keeps_the_phase_metadata_when_the_step_returns_a_new_dict(pipeline):
    """``unit`` returns a new dict without metadata; the result and the
    next step in the phase still carry the phase's metadata."""
    with Engine() as e:
        e.register("p", pipeline("compartment", then_double=True), fresh_output())
        submit_tiles(e, [1, 2], {"carrier": 1, "compartment": 1}, complete="compartment")
        results = scoped(e, 1)

    (compartment,) = [r for r in results if phase_of(r) == 1]
    assert compartment["metadata"]["unit"] == {
        "compartment": 1
    }  # no levels: carrier is not a phase
    assert compartment["metadata"]["scope_level"] == "compartment"
    assert compartment["doubled"] == 6


def test_lineage_lists_a_failed_tile_and_a_failed_compartment(pipeline):
    with Engine() as e:
        e.register("p", pipeline("compartment", "carrier"), fresh_output())
        e.submit("p", {"fail": True}, scope={"carrier": 1, "compartment": 1})  # job 1
        submit_tiles(
            e, [1], {"carrier": 1, "compartment": 1}, complete="compartment"
        )  # job 2, scope job 3
        submit_tiles(
            e,
            [666],
            {"carrier": 1, "compartment": 2},  # job 4, scope jobs 5 and 6
            complete=["compartment", "carrier"],
        )
        results = scoped(e, 2)

    carrier = by_unit(results, "carrier")[(("carrier", 1),)]
    failed = sorted(
        (f["step"], f["phase"], f["submission_idx"]) for f in carrier["lineage"]["failed"]
    )
    assert failed == [("tile", 0, 1), ("unit", 1, 4)]
    assert carrier["lineage"]["submissions"] == [2]


# ---- The engine's own scope cases, one each ---------------------------


class TestEngineScopes:
    def test_two_carriers_at_once_keep_their_compartments_apart(self):
        """Tiles of two carriers arrive interleaved, with the same
        compartment numbers on both. Each compartment is summed, then each
        carrier sums only its own compartments."""
        _temp_step(
            """
            def run(pd, state, **p):
                pd["value"] = pd["input"]["value"]
                return pd
        """,
            name="carriers_tile",
        )
        _temp_step(
            """
            import time
            def run(pd, state, **p):
                time.sleep(0.2)   # a slow compartment, so a carrier signal could overtake it
                return {"compartment_sum": sum(r["value"] for r in pd["results"]),
                        "compartment": pd["metadata"]["scope"]["compartment"]}
        """,
            name="carriers_compartment",
        )
        _temp_step(
            """
            def run(pd, state, **p):
                return {"carrier_sum": sum(r["compartment_sum"] for r in pd["results"]),
                        "compartments": sorted(r["compartment"] for r in pd["results"]),
                        "carrier": pd["metadata"]["scope"]["carrier"]}
        """,
            name="carriers_carrier",
        )
        yaml = _temp_yaml(
            "wf:\n  - carriers_tile:\n"
            "  - carriers_compartment:\n      scope: compartment\n"
            "  - carriers_carrier:\n      scope: carrier"
        )
        from zmart_analysis import Engine

        with Engine(max_concurrent=8) as e:
            e.register("test", yaml, fresh_output())
            layout = [(1, 1, 1), (2, 1, 100), (1, 2, 2), (2, 2, 200)]
            for carrier, compartment, value in layout:
                for _ in range(3):
                    e.submit(
                        "test",
                        {"value": value},
                        scope={"carrier": carrier, "compartment": compartment},
                    )
                e.submit(
                    "test",
                    {"value": 0},
                    scope={"carrier": carrier, "compartment": compartment},
                    complete="compartment",
                )
            e.submit(
                "test", {"value": 0}, scope={"carrier": 1, "compartment": 2}, complete="carrier"
            )
            e.submit(
                "test", {"value": 0}, scope={"carrier": 2, "compartment": 2}, complete="carrier"
            )
            # Each carrier's closing tile lands in its compartment 2 after
            # that compartment closed, so it opens it again; completing the
            # carrier closes it a second time, on that tile alone.
            results = _wait_for_results(e, "test", 18 + 4 + 2 + 2, timeout=60)
        carriers = {r["carrier"]: r for r in results if "carrier_sum" in r}
        assert carriers[1]["carrier_sum"] == 3 * 1 + 3 * 2
        assert carriers[2]["carrier_sum"] == 3 * 100 + 3 * 200
        assert carriers[1]["compartments"] == [1, 2, 2]
        assert carriers[2]["compartments"] == [1, 2, 2]

    def test_scope_collects_results(self):
        """Scoped step receives accumulated results from all jobs."""
        _temp_step(
            """
            def run(pd, state, **p):
                pd["tile"] = pd["input"]["tile"]
                return pd
        """,
            name="sc_seg",
        )
        _temp_step(
            """
            def run(pd, state, **p):
                tiles = [r["tile"] for r in pd["results"]]
                pd["tiles"] = sorted(tiles)
                return pd
        """,
            name="sc_stitch",
        )
        yaml = _temp_yaml("""
            wf:
              - sc_seg:
              - sc_stitch:
                  scope: group
        """)
        from zmart_analysis import Engine

        with Engine() as e:
            e.register("test", yaml, fresh_output())
            for i in range(3):
                complete = "group" if i == 2 else None
                e.submit("test", {"tile": i}, scope={"group": "R1"}, complete=complete)
            results = _wait_for_results(e, "test", 4, timeout=15)

        # Should have 3 Phase 0 results + 1 scoped result
        phase0 = [r for r in results if phase_of(r) == 0]
        scoped = [r for r in results if phase_of(r) == 1]
        assert len(phase0) == 3
        assert len(scoped) == 1
        assert scoped[0]["tiles"] == [0, 1, 2]

    def test_scope_preserves_submission_order(self):
        _temp_step(
            """
            import time
            def run(pd, state, **p):
                time.sleep(0.05)
                pd["val"] = pd["input"]["val"]
                return pd
        """,
            name="ord_step",
        )
        _temp_step(
            """
            def run(pd, state, **p):
                pd["order"] = [r["val"] for r in pd["results"]]
                return pd
        """,
            name="ord_collect",
        )
        yaml = _temp_yaml("""
            wf:
              - ord_step:
              - ord_collect:
                  scope: group
        """)
        from zmart_analysis import Engine

        with Engine() as e:
            e.register("test", yaml, fresh_output())
            for i in range(5):
                complete = "group" if i == 4 else None
                e.submit("test", {"val": i}, scope={"group": "G1"}, complete=complete)
            results = _wait_for_results(e, "test", 6, timeout=20)

        scoped = [r for r in results if phase_of(r) == 1]
        assert len(scoped) == 1
        assert scoped[0]["order"] == [0, 1, 2, 3, 4]

    def test_multiple_scope_groups(self):
        """Different scope groups are collected independently."""
        _temp_step(
            """
            def run(pd, state, **p):
                pd["val"] = pd["input"]["val"]
                return pd
        """,
            name="mg_step",
        )
        _temp_step(
            """
            def run(pd, state, **p):
                pd["vals"] = sorted([r["val"] for r in pd["results"]])
                return pd
        """,
            name="mg_collect",
        )
        yaml = _temp_yaml("""
            wf:
              - mg_step:
              - mg_collect:
                  scope: group
        """)
        from zmart_analysis import Engine

        with Engine() as e:
            e.register("test", yaml, fresh_output())
            # Group A: values 10, 20
            e.submit("test", {"val": 10}, scope={"group": "A"})
            e.submit("test", {"val": 20}, scope={"group": "A"}, complete="group")
            # Group B: values 30, 40, 50
            e.submit("test", {"val": 30}, scope={"group": "B"})
            e.submit("test", {"val": 40}, scope={"group": "B"})
            e.submit("test", {"val": 50}, scope={"group": "B"}, complete="group")
            results = _wait_for_results(e, "test", 7, timeout=20)

        scoped = [r for r in results if phase_of(r) == 1]
        scoped_vals = sorted([tuple(r["vals"]) for r in scoped])
        assert (10, 20) in scoped_vals
        assert (30, 40, 50) in scoped_vals

    def test_complete_list(self):
        """complete parameter accepts a list of scope levels."""
        _temp_step(
            """
            def run(pd, state, **p):
                pd["v"] = pd["input"]["v"]
                return pd
        """,
            name="cl_step",
        )
        _temp_step(
            """
            def run(pd, state, **p):
                pd["group_vals"] = [r["v"] for r in pd["results"]]
                return pd
        """,
            name="cl_group",
        )
        _temp_step(
            """
            def run(pd, state, **p):
                pd["all_vals"] = [r.get("group_vals", [])
                                   for r in pd["results"]]
                return pd
        """,
            name="cl_all",
        )
        yaml = _temp_yaml("""
            wf:
              - cl_step:
              - cl_group:
                  scope: group
              - cl_all:
                  scope: all
        """)
        from zmart_analysis import Engine

        with Engine() as e:
            e.register("test", yaml, fresh_output())
            e.submit("test", {"v": 1}, scope={"group": "G1"})
            e.submit("test", {"v": 2}, scope={"group": "G1"}, complete=["group", "all"])
            results = _wait_for_results(e, "test", 4, timeout=20)

        phase2 = [r for r in results if phase_of(r) == 2]
        assert len(phase2) == 1

    def test_all_scope_collects_everything(self):
        """Scope 'all' (not a key in any scope dict) collects everything."""
        _temp_step(
            """
            def run(pd, state, **p):
                pd["v"] = pd["input"]["v"]
                return pd
        """,
            name="al_step",
        )
        _temp_step(
            """
            def run(pd, state, **p):
                pd["total"] = sum(r["v"] for r in pd["results"])
                return pd
        """,
            name="al_sum",
        )
        yaml = _temp_yaml("""
            wf:
              - al_step:
              - al_sum:
                  scope: all
        """)
        from zmart_analysis import Engine

        with Engine() as e:
            e.register("test", yaml, fresh_output())
            e.submit("test", {"v": 10}, scope={"group": "A"})
            e.submit("test", {"v": 20}, scope={"group": "B"})
            e.submit("test", {"v": 30}, scope={"group": "C"}, complete="all")
            results = _wait_for_results(e, "test", 4, timeout=15)

        scoped = [r for r in results if phase_of(r) == 1]
        assert len(scoped) == 1
        assert scoped[0]["total"] == 60

    def test_failures_reach_scoped_step(self):
        """Phase 0 failures are aggregated into pipeline_data['failures']
        for the scoped step to inspect."""
        _temp_step(
            """
            def run(pd, state, **p):
                if pd["input"]["v"] == 99:
                    raise ValueError("deliberate failure")
                pd["v"] = pd["input"]["v"]
                return pd
        """,
            name="fr_step",
        )
        _temp_step(
            """
            def run(pd, state, **p):
                pd["n_results"] = len(pd["results"])
                pd["n_failures"] = len(pd["failures"])
                pd["failure_steps"] = [f.get("step") for f in pd["failures"]]
                pd["failure_errors"] = [f.get("error") for f in pd["failures"]]
                return pd
        """,
            name="fr_collect",
        )
        yaml = _temp_yaml("""
            wf:
              - fr_step:
              - fr_collect:
                  scope: group
        """)
        from zmart_analysis import Engine

        with Engine() as e:
            e.register("test", yaml, fresh_output())
            e.submit("test", {"v": 1}, scope={"group": "G"})
            e.submit("test", {"v": 99}, scope={"group": "G"})
            e.submit("test", {"v": 2}, scope={"group": "G"}, complete="group")
            results = _wait_for_results(e, "test", 3, timeout=15)

        scoped = [r for r in results if phase_of(r) == 1]
        assert len(scoped) == 1
        assert scoped[0]["n_results"] == 2
        assert scoped[0]["n_failures"] == 1
        assert scoped[0]["failure_steps"] == ["fr_step"]
        assert "deliberate failure" in scoped[0]["failure_errors"][0]

    def test_scope_collection_prunes_consumed_failures(self):
        """A failure handed to its unit is not handed again when the unit
        closes a second time; another unit's failure waits for its own."""
        _temp_step(
            """
            def run(pd, state, **p):
                if pd["input"].get("fail"):
                    raise ValueError(f"failed {pd['input']['group']}")
                pd["group"] = pd["input"]["group"]
                return pd
        """,
            name="pf_step",
        )
        _temp_step(
            """
            def run(pd, state, **p):
                pd["n_results"] = len(pd["results"])
                pd["failure_errors"] = [f["error"] for f in pd["failures"]]
                return pd
        """,
            name="pf_collect",
        )
        yaml = _temp_yaml("""
            wf:
              - pf_step:
              - pf_collect:
                  scope: group
        """)
        from zmart_analysis import Engine

        with Engine() as e:
            e.register("test", yaml, fresh_output())
            e.submit("test", {"group": "A", "fail": True}, scope={"group": "A"})
            e.submit("test", {"group": "B", "fail": True}, scope={"group": "B"})
            _wait_for_status(e, "test", expected_total=2, timeout=15)

            e.submit("test", {"group": "A", "fail": False}, scope={"group": "A"}, complete="group")
            _wait_for_results(e, "test", 2, timeout=15)
            e.submit("test", {"group": "A", "fail": False}, scope={"group": "A"}, complete="group")
            e.submit("test", {"group": "B", "fail": False}, scope={"group": "B"}, complete="group")
            results = _wait_for_results(e, "test", 6, timeout=15)

        scoped = [r for r in results if phase_of(r) == 1]
        assert [r["metadata"]["scope"]["group"] for r in scoped] == ["A", "A", "B"]
        first_a, second_a, b = scoped
        assert first_a["n_results"] == 1
        assert len(first_a["failure_errors"]) == 1
        assert "failed A" in first_a["failure_errors"][0]
        assert second_a["n_results"] == 1
        assert second_a["failure_errors"] == []
        assert len(b["failure_errors"]) == 1
        assert "failed B" in b["failure_errors"][0]


# ---- Engine (environment isolation) ----------------------------------
