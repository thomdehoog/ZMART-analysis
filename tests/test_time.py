"""
Scopes over time.

An image's scope may name its ``timepoint``, a whole number the acquisition
gives. A scoped step's unit is then its place at that timepoint, and a step
with ``timepoints: N`` (N of 2 or more) also sees the N-1 timepoints before
it, in timepoint order per place, with its own answer at the timepoint
before as ``previous``.

The steps run on the engine's own Python, so no conda environment is needed.
"""

import time

import pytest
from helpers import _write_step as write_step
from helpers import _write_yaml as write_yaml
from helpers import fresh_output

from zmart_analysis import ScopeError

#: Per image: hands on its value; waits for "delay" seconds first.
TILE = """
    import time

    def run(pd, state, **p):
        time.sleep(pd["input"].get("delay", 0))
        pd["value"] = pd["input"]["value"]
        return pd
"""

#: Over one unit at one timepoint: the values it got.
COUNT = """
    def run(pd, state, **p):
        return {
            "unit": pd["metadata"]["unit"],
            "values": sorted(part["output"]["value"] for part in pd["parts"]),
        }
"""

#: Over a window of timepoints: what it got, in order, and what it had before.
#: Notes the order it ran in, in the log the input names.
TRACK = """
    def run(pd, state, **p):
        groups = pd["parts"]
        now = pd["metadata"]["unit"]["timepoint"]
        values = [[part["output"]["value"] for part in g["parts"]] for g in groups]
        if 666 in values[-1]:
            raise ValueError("lost track")
        log = next((part.get("input", {}).get("log") for g in groups for part in g["parts"]), None)
        if log:
            with open(log, "a") as f:
                f.write(f"{now}\\n")
        previous = pd["previous"]
        return {
            "timepoint": now,
            "timepoints": [g["timepoint"] for g in groups],
            "values": values,
            "previous": None if previous is None else previous["timepoint"],
        }
"""


@pytest.fixture(scope="module", autouse=True)
def steps():
    for name, code in {"time_tile": TILE, "time_count": COUNT, "time_track": TRACK}.items():
        write_step(code, name=name)


def pipeline(text):
    return write_yaml(text)


TRACKING = """
    metadata:
      levels: [experiment, carrier, compartment, position]
    wf:
      - time_tile:
      - time_track:
          scope: position
          timepoints: 2
"""


def scoped_outputs(board, step):
    """The outputs of the done jobs of *step*, in job order."""
    return [
        board[job]["output"]
        for job in sorted(board)
        if board[job]["step"] == step and board[job]["state"] == "done"
    ]


# ---- timepoints: 1 -------------------------------------------------------


def test_a_unit_at_one_timepoint_keeps_timepoints_apart(engine_factory):
    engine = engine_factory()
    engine.register(
        "wf",
        pipeline("wf:\n  - time_tile:\n  - time_count:\n      scope: compartment\n"),
        fresh_output(),
    )
    engine.submit("wf", {"value": 1}, scope={"compartment": 1, "timepoint": 0})
    engine.submit(
        "wf", {"value": 2}, scope={"compartment": 1, "timepoint": 0}, complete="compartment"
    )
    engine.submit(
        "wf", {"value": 5}, scope={"compartment": 1, "timepoint": 1}, complete="compartment"
    )
    board = engine.deregister("wf")

    counts = scoped_outputs(board, "time_count")
    assert [(c["unit"], c["values"]) for c in counts] == [
        ({"compartment": 1, "timepoint": 0}, [1, 2]),
        ({"compartment": 1, "timepoint": 1}, [5]),
    ]


def test_complete_timepoint_closes_that_round_and_no_other(engine_factory):
    engine = engine_factory()
    engine.register(
        "wf",
        pipeline("wf:\n  - time_tile:\n  - time_count:\n      scope: compartment\n"),
        fresh_output(),
    )
    engine.submit("wf", {"value": 1}, scope={"compartment": 1, "timepoint": 0})
    engine.submit("wf", {"value": 9}, scope={"compartment": 1, "timepoint": 1})
    engine.submit(
        "wf", {"value": 2}, scope={"compartment": 2, "timepoint": 0}, complete="timepoint"
    )
    # Compartment 1 at timepoint 1 is never closed, so it never runs.
    board = engine.deregister("wf")
    closed = [c["unit"] for c in scoped_outputs(board, "time_count")]

    assert sorted(closed, key=lambda u: u["compartment"]) == [
        {"compartment": 1, "timepoint": 0},
        {"compartment": 2, "timepoint": 0},
    ]


def test_a_step_over_everything_runs_once_per_timepoint(engine_factory):
    engine = engine_factory()
    engine.register(
        "wf",
        pipeline(
            """
            metadata:
              levels: [experiment, compartment]
            wf:
              - time_tile:
              - time_count:
                  scope: experiment
            """
        ),
        fresh_output(),
    )
    for t in (0, 1):
        engine.submit("wf", {"value": 10 * t + 1}, scope={"compartment": 1, "timepoint": t})
        engine.submit(
            "wf",
            {"value": 10 * t + 2},
            scope={"compartment": 2, "timepoint": t},
            complete="timepoint",
        )
    board = engine.deregister("wf")

    counts = scoped_outputs(board, "time_count")
    assert [(c["unit"], c["values"]) for c in counts] == [
        ({"timepoint": 0}, [1, 2]),
        ({"timepoint": 1}, [11, 12]),
    ]


def test_complete_timepoint_needs_a_timepoint(engine_factory):
    engine = engine_factory()
    engine.register(
        "wf",
        pipeline("wf:\n  - time_tile:\n  - time_count:\n      scope: compartment\n"),
        fresh_output(),
    )
    with pytest.raises(ScopeError, match="timepoint"):
        engine.submit("wf", {"value": 1}, scope={"compartment": 1}, complete="timepoint")


# ---- timepoints: N -------------------------------------------------------


def track(engine, positions, values=None, delays=None, log=None, compartment=3):
    """Submit one image per (position, timepoint) pair, in that order."""
    jobs = []
    for position, t in positions:
        data = {"value": (values or {}).get((position, t), t)}
        if delays:
            data["delay"] = delays.get((position, t), 0)
        if log:
            data["log"] = str(log)
        scope = {"carrier": 1, "compartment": compartment, "position": position, "timepoint": t}
        jobs.append(engine.submit("wf", data, scope=scope))
    return jobs


def test_a_window_sees_this_timepoint_and_the_one_before(engine_factory):
    engine = engine_factory()
    engine.register("wf", pipeline(TRACKING), fresh_output())
    track(engine, [(7, 0), (7, 1), (7, 2)])
    board = engine.deregister("wf")

    tracked = scoped_outputs(board, "time_track")
    assert [t["timepoints"] for t in tracked] == [[0], [0, 1], [1, 2]]
    assert [t["values"] for t in tracked] == [[[0]], [[0], [1]], [[1], [2]]]
    assert [t["previous"] for t in tracked] == [None, 0, 1]


def test_after_a_skipped_round_the_next_timepoint_follows_the_last_one_there(engine_factory):
    engine = engine_factory()
    engine.register("wf", pipeline(TRACKING), fresh_output())
    track(engine, [(7, 0), (7, 2)])
    board = engine.deregister("wf")

    tracked = scoped_outputs(board, "time_track")
    # Timepoint 1 was never taken: missing from the window, not empty.
    assert [t["timepoints"] for t in tracked] == [[0], [2]]
    assert [t["previous"] for t in tracked] == [None, 0]


def test_the_same_position_in_two_wells_is_two_tracks(engine_factory):
    engine = engine_factory()
    engine.register("wf", pipeline(TRACKING), fresh_output())
    track(engine, [(7, 0), (7, 1)], compartment=3)
    track(engine, [(7, 0), (7, 1)], values={(7, 0): 30, (7, 1): 31}, compartment=4)
    board = engine.deregister("wf")

    tracked = scoped_outputs(board, "time_track")
    assert sorted(t["values"] for t in tracked if t["timepoint"] == 1) == [
        [[0], [1]],
        [[30], [31]],
    ]


def test_a_track_runs_in_timepoint_order_when_a_later_image_is_faster(engine_factory, tmp_path):
    log = tmp_path / "order"
    engine = engine_factory(max_concurrent=4)
    engine.register(
        "wf",
        pipeline(
            """
            metadata:
              levels: [experiment, carrier, compartment, position]
            wf:
              - time_tile:
                  max_workers: 4
              - time_track:
                  scope: position
                  timepoints: 2
            """
        ),
        fresh_output(),
    )
    track(engine, [(7, 0), (7, 1)], delays={(7, 0): 1.5}, log=log)
    board = engine.deregister("wf")

    assert log.read_text().split() == ["0", "1"]
    assert [t["previous"] for t in scoped_outputs(board, "time_track")] == [None, 0]


def test_tracks_finish_with_one_thread(engine_factory):
    engine = engine_factory(max_concurrent=1)
    engine.register("wf", pipeline(TRACKING), fresh_output())
    track(engine, [(p, t) for t in range(3) for p in (1, 2)])
    board = engine.deregister("wf")

    assert len(scoped_outputs(board, "time_track")) == 6


def test_previous_is_none_after_a_failed_timepoint(engine_factory):
    engine = engine_factory()
    engine.register("wf", pipeline(TRACKING), fresh_output())
    track(engine, [(7, 0), (7, 1), (7, 2)], values={(7, 1): 666})
    board = engine.deregister("wf")

    tracked = scoped_outputs(board, "time_track")
    assert [t["timepoint"] for t in tracked] == [0, 2]
    assert tracked[1]["previous"] is None
    failed = [entry for entry in board.values() if entry["state"] == "failed"]
    assert [entry["step"] for entry in failed] == ["time_track"]


def test_a_late_image_does_not_run_a_later_window_again(engine_factory):
    engine = engine_factory()
    engine.register("wf", pipeline(TRACKING), fresh_output())
    track(engine, [(7, 0), (7, 1)])
    time.sleep(1.0)
    track(engine, [(7, 0)], values={(7, 0): 100})
    board = engine.deregister("wf")

    tracked = scoped_outputs(board, "time_track")
    assert [t["timepoint"] for t in tracked] == [0, 1, 0]


def test_what_no_window_needs_any_more_is_let_go(engine_factory):
    engine = engine_factory()
    engine.register("wf", pipeline(TRACKING), fresh_output())
    track(engine, [(7, t) for t in range(6)])
    windows = engine.pipelines["wf"].units.windows
    engine.deregister("wf")  # returns when every window has run
    kept = windows.kept()

    # One timepoint before the last is all a window of two still needs.
    assert kept == 1


# ---- What register refuses -------------------------------------------------


@pytest.mark.parametrize(
    "text, reason",
    [
        ("wf:\n  - time_tile:\n      timepoints: 2\n", "scope"),
        (
            "wf:\n  - time_tile:\n  - time_count:\n      scope: group\n      timepoints: all\n",
            "version",
        ),
        (
            "wf:\n  - time_tile:\n  - time_count:\n      scope: group\n      timepoints: 0\n",
            "whole number",
        ),
        (
            "metadata:\n  levels: [compartment, position]\n"
            "wf:\n  - time_tile:\n  - time_count:\n      scope: position\n",
            "timepoints",
        ),
        (
            "metadata:\n  levels: [compartment]\n"
            "wf:\n  - time_tile:\n  - time_track:\n      scope: position\n      timepoints: 2\n",
            "levels",
        ),
        (
            "metadata:\n  levels: [carrier, compartment]\n"
            "wf:\n  - time_tile:\n  - time_track:\n      scope: compartment\n      timepoints: 2\n"
            "  - time_count:\n      scope: carrier\n",
            "last",
        ),
        (
            "metadata:\n  levels: [compartment, position]\n"
            "wf:\n  - time_tile:\n  - time_count:\n      scope: compartment\n"
            "  - time_track:\n      scope: position\n      timepoints: 2\n",
            "first",
        ),
    ],
)
def test_register_refuses(engine_factory, text, reason):
    engine = engine_factory()
    with pytest.raises(ValueError, match=reason):
        engine.register("wf", pipeline(text), fresh_output())


def test_a_pipeline_without_time_keeps_its_units(engine_factory):
    """No timepoint in the scopes: the units of 0.5."""
    engine = engine_factory()
    engine.register(
        "wf",
        pipeline("wf:\n  - time_tile:\n  - time_count:\n      scope: compartment\n"),
        fresh_output(),
    )
    engine.submit("wf", {"value": 1}, scope={"compartment": 1})
    engine.submit("wf", {"value": 2}, scope={"compartment": 1}, complete="compartment")
    board = engine.deregister("wf")

    (count,) = scoped_outputs(board, "time_count")
    assert count["unit"] == {"compartment": 1}
    assert count["values"] == [1, 2]
