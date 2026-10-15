"""
The job board, deregister, and workers that live with their pipeline.

``submit`` returns a job id. ``status(name)`` is the board: every job of a
pipeline by id, with the step it is at, its scope, its state and its output.
``deregister(name)`` ends a pipeline: its jobs finish (or are cancelled), the
workers no other pipeline needs are stopped, and the final board is handed
back. A worker is never stopped for being idle.

The steps run on the engine's own Python, so no conda environment is needed.
A gated step writes its pid to a file and then waits for a second file, so a
test can hold a job in ``running`` for as long as it needs to look at it.

Usage
-----
    python -m pytest tests/test_job_board.py -v
"""

import os
import threading
import time
from pathlib import Path

import pytest
from helpers import _SESSION_TEMP, _next_id
from helpers import _write_step as write_step
from helpers import _write_yaml as write_yaml

from zmart_analysis import Engine

PASS = "def run(pd, state, **p): return pd"

DOUBLE = """
    def run(pd, state, **p):
        pd["double"] = {"value": pd["input"]["value"] * 2}
        return pd
"""

ADD_ONE = """
    def run(pd, state, **p):
        pd["add_one"] = {"value": pd["double"]["value"] + 1}
        return pd
"""

BREAK = """
    def run(pd, state, **p):
        raise ValueError("this step breaks on purpose")
"""

# Writes its pid to input["started"], then waits until input["gate"] exists.
GATED = """
    import os, time

    def run(pd, state, **p):
        started = pd["input"].get("started")
        if started:
            with open(started, "w") as f:
                f.write(str(os.getpid()))
        gate = pd["input"].get("gate")
        while gate and not os.path.exists(gate):
            time.sleep(0.02)
        pd["gated"] = {"pid": os.getpid()}
        return pd
"""

# Adds up the values of the images of one unit.
TOTAL = """
    def run(pd, state, **p):
        return {"total": sum(r["gated"]["value"] for r in pd["results"])}
"""

GATED_WITH_VALUE = """
    import os, time

    def run(pd, state, **p):
        gate = pd["input"].get("gate")
        while gate and not os.path.exists(gate):
            time.sleep(0.02)
        pd["gated"] = {"value": pd["input"]["value"]}
        return pd
"""


@pytest.fixture(scope="module", autouse=True)
def steps():
    for name, code in {
        "board_pass": PASS,
        "double": DOUBLE,
        "add_one": ADD_ONE,
        "board_break": BREAK,
        "gated": GATED,
        "total": TOTAL,
        "gated_value": GATED_WITH_VALUE,
    }.items():
        write_step(code, name=name)


def a_file():
    """A path in the session folder that does not exist yet."""
    return str(_SESSION_TEMP / f"board_{_next_id()}")


def open_gate(path):
    Path(path).write_text("open")


def wait_for_file(path, timeout=20):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if os.path.exists(path) and Path(path).read_text().strip():
            return int(Path(path).read_text())
        time.sleep(0.02)
    raise AssertionError(f"{path} never appeared")


def wait_for_state(engine, name, job, states, timeout=30):
    """Poll the board until *job* is in one of *states*; return its entry."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        entry = engine.status(name).get(job)
        if entry is not None and entry["state"] in states:
            return entry
        time.sleep(0.02)
    raise AssertionError(f"job {job} never reached {states}: {engine.status(name).get(job)}")


def alive(pid):
    """Whether a process is still running, on either platform."""
    from test_engine import _alive

    return _alive(pid)


# ---- ids ---------------------------------------------------------------


def test_submit_returns_ids_counted_per_pipeline(engine_factory):
    pipeline = write_yaml("wf:\n  - board_pass:")
    engine = engine_factory()
    engine.register("first", pipeline)
    engine.register("second", pipeline)

    ids = [engine.submit(pipeline="first", data={}) for _ in range(3)]
    other = engine.submit(pipeline="second", data={})

    assert ids == [1, 2, 3]
    assert other == 1


# ---- the board -----------------------------------------------------------


def test_a_job_on_the_board_has_step_scope_state_and_output(engine_factory):
    pipeline = write_yaml("wf:\n  - double:\n  - add_one:")
    engine = engine_factory()
    engine.register("wf", pipeline)

    job = engine.submit(pipeline="wf", data={"value": 20})
    entry = wait_for_state(engine, "wf", job, {"done"})

    assert set(entry) == {"step", "scope", "state", "output"}
    assert entry["step"] == "add_one"
    assert entry["scope"] == {}
    assert entry["output"]["double"] == {"value": 40}
    assert entry["output"]["add_one"] == {"value": 41}
    assert entry["output"]["input"] == {"value": 20}
    assert not [key for key in entry["output"] if key.startswith("_")]


def test_a_job_is_queued_then_running_at_its_step_then_done(engine_factory):
    pipeline = write_yaml("wf:\n  - gated:\n  - board_pass:")
    engine = engine_factory(max_concurrent=1)
    engine.register("wf", pipeline)
    started, gate = a_file(), a_file()

    first = engine.submit(pipeline="wf", data={"started": started, "gate": gate})
    second = engine.submit(pipeline="wf", data={})
    wait_for_file(started)

    board = engine.status("wf")
    assert board[first]["state"] == "running"
    assert board[first]["step"] == "gated"
    assert board[first]["output"] is None
    assert board[second]["state"] == "queued"
    assert board[second]["step"] == "gated"

    open_gate(gate)
    assert wait_for_state(engine, "wf", first, {"done"})["step"] == "board_pass"
    assert wait_for_state(engine, "wf", second, {"done"})["state"] == "done"


def test_a_failing_step_fails_its_job_with_the_error(engine_factory):
    pipeline = write_yaml("wf:\n  - board_pass:\n  - board_break:")
    engine = engine_factory()
    engine.register("wf", pipeline)

    job = engine.submit(pipeline="wf", data={})
    entry = wait_for_state(engine, "wf", job, {"failed"})

    assert entry["step"] == "board_break"
    assert entry["output"] is None
    assert "this step breaks on purpose" in entry["error"]


def test_status_without_a_name_gives_one_board_per_pipeline(engine_factory):
    pipeline = write_yaml("wf:\n  - board_pass:")
    engine = engine_factory()
    engine.register("first", pipeline)
    engine.register("second", pipeline)
    job = engine.submit(pipeline="first", data={})
    wait_for_state(engine, "first", job, {"done"})

    boards = engine.status()

    assert set(boards) == {"first", "second"}
    assert boards["first"][1]["state"] == "done"
    assert boards["second"] == {}


def test_an_unknown_pipeline_raises_key_error(engine_factory):
    engine = engine_factory()
    with pytest.raises(KeyError):
        engine.submit(pipeline="nothing", data={})
    with pytest.raises(KeyError):
        engine.status("nothing")
    with pytest.raises(KeyError):
        engine.deregister("nothing")


def test_results_is_gone(engine_factory):
    assert not hasattr(engine_factory(), "results")


# ---- scoped jobs -----------------------------------------------------------


def test_completing_a_unit_adds_a_waiting_scope_job_that_runs_on_its_images(engine_factory):
    pipeline = write_yaml("""
        metadata:
          levels: [carrier, group]
        wf:
          - gated_value:
          - total:
              scope: group
    """)
    engine = engine_factory()
    engine.register("wf", pipeline)
    gate = a_file()

    def image(value, position, complete=None):
        return engine.submit(
            pipeline="wf",
            data={"value": value, "gate": gate},
            scope={"carrier": 1, "group": 2, "position": position},
            complete=complete,
        )

    first = image(10, 1)
    second = image(20, 2)
    last = image(30, 3, complete="group")

    board = engine.status("wf")
    assert [first, second, last] == [1, 2, 3]
    assert board[4] == {
        "step": "total",
        "scope": {"carrier": 1, "group": 2},
        "state": "waiting",
        "output": None,
    }
    assert board[last]["scope"] == {"carrier": 1, "group": 2, "position": 3}

    open_gate(gate)
    entry = wait_for_state(engine, "wf", 4, {"done"})
    assert entry["output"]["total"] == 60
    assert entry["output"]["lineage"]["submissions"] == [1, 2, 3]


# ---- deregister ------------------------------------------------------------


def test_deregister_finishes_the_jobs_and_returns_the_final_board(engine_factory):
    pipeline = write_yaml("wf:\n  - gated:")
    engine = engine_factory()
    engine.register("wf", pipeline)
    gate = a_file()
    job = engine.submit(pipeline="wf", data={"gate": gate})

    threading.Timer(0.3, open_gate, args=(gate,)).start()
    board = engine.deregister("wf")

    assert board[job]["state"] == "done"
    assert "wf" not in engine.status()
    with pytest.raises(KeyError):
        engine.submit(pipeline="wf", data={})


def test_deregister_stops_a_worker_no_other_pipeline_needs(engine_factory):
    pipeline = write_yaml("wf:\n  - gated:")
    engine = engine_factory()
    engine.register("wf", pipeline)
    started = a_file()
    job = engine.submit(pipeline="wf", data={"started": started})
    pid = wait_for_file(started)
    wait_for_state(engine, "wf", job, {"done"})

    engine.deregister("wf")

    time.sleep(0.5)
    assert not alive(pid)


def test_deregister_keeps_a_worker_another_pipeline_needs(engine_factory):
    pipeline = write_yaml("wf:\n  - gated:")
    engine = engine_factory()
    engine.register("leaving", pipeline)
    engine.register("staying", pipeline)
    started = a_file()
    job = engine.submit(pipeline="leaving", data={"started": started})
    pid = wait_for_file(started)
    wait_for_state(engine, "leaving", job, {"done"})

    engine.deregister("leaving")

    time.sleep(0.5)
    assert alive(pid)
    again = engine.submit(pipeline="staying", data={})
    assert wait_for_state(engine, "staying", again, {"done"})["output"]["gated"]["pid"] == pid


def test_deregister_without_waiting_cancels_running_and_queued_jobs(engine_factory):
    pipeline = write_yaml("wf:\n  - gated:")
    engine = engine_factory(max_concurrent=1)
    engine.register("wf", pipeline)
    started, gate = a_file(), a_file()
    running = engine.submit(pipeline="wf", data={"started": started, "gate": gate})
    queued = engine.submit(pipeline="wf", data={})
    pid = wait_for_file(started)

    t0 = time.monotonic()
    board = engine.deregister("wf", wait=False)

    assert time.monotonic() - t0 < 5.0
    assert board[running]["state"] == "cancelled"
    assert board[queued]["state"] == "cancelled"
    time.sleep(0.5)
    assert not alive(pid)


def test_deregister_without_waiting_cancels_a_waiting_scope_job(engine_factory):
    pipeline = write_yaml("""
        wf:
          - gated_value:
          - total:
              scope: group
    """)
    engine = engine_factory()
    engine.register("wf", pipeline)
    gate = a_file()
    engine.submit(pipeline="wf", data={"value": 1, "gate": gate}, scope={"group": 1})
    engine.submit(
        pipeline="wf", data={"value": 2, "gate": gate}, scope={"group": 1}, complete="group"
    )
    assert engine.status("wf")[3]["state"] == "waiting"

    board = engine.deregister("wf", wait=False)

    assert board[3]["state"] == "cancelled"


def test_a_name_can_be_registered_again_and_counts_from_one(engine_factory):
    pipeline = write_yaml("wf:\n  - board_pass:")
    engine = engine_factory()
    engine.register("wf", pipeline)
    engine.submit(pipeline="wf", data={})
    engine.deregister("wf")

    engine.register("wf", pipeline)

    assert engine.submit(pipeline="wf", data={}) == 1


# ---- workers stay -----------------------------------------------------------


def test_the_engine_has_no_idle_timeout():
    with pytest.raises(TypeError):
        Engine(idle_timeout=300)


def test_a_worker_is_still_the_same_after_a_pause(engine_factory):
    pipeline = write_yaml("wf:\n  - gated:")
    engine = engine_factory()
    engine.register("wf", pipeline)
    first = engine.submit(pipeline="wf", data={})
    pid = wait_for_state(engine, "wf", first, {"done"})["output"]["gated"]["pid"]

    time.sleep(1.0)
    second = engine.submit(pipeline="wf", data={})

    assert wait_for_state(engine, "wf", second, {"done"})["output"]["gated"]["pid"] == pid


def test_shutdown_deregisters_every_pipeline(engine_factory):
    pipeline = write_yaml("wf:\n  - board_pass:")
    engine = engine_factory()
    engine.register("first", pipeline)
    engine.register("second", pipeline)

    engine.shutdown()

    assert engine.status() == {}
