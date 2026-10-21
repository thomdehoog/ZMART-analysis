"""
Steps that run together: a chain.

Consecutive steps of a phase with the same environment and the same
``max_workers`` run as one call to one worker. The worker hands the output
of one step to the next in memory; only the chain's last output is written.
Each step still has its own timeout, its own record and provenance, and
names itself in an error.

The steps run on the engine's own Python, so no conda environment is needed.
"""

import json
import time
from pathlib import Path

import pytest
from helpers import _write_step as write_step
from helpers import _write_yaml as write_yaml
from helpers import fresh_output

STEPS = {
    # Adds one to "n"; keeps the whole dictionary.
    "chain_add": """
        def run(pd, state, **p):
            pd["n"] = pd.get("n", pd["input"].get("n", 0)) + 1
            return pd
    """,
    # Hands on a new dictionary: the provenance of the steps before is not in it.
    "chain_fresh": """
        def run(pd, state, **p):
            return {"n": pd["n"] * 10}
    """,
    "chain_break": """
        def run(pd, state, **p):
            raise ValueError("broken in the middle")
    """,
    # Writes a file when it runs, so a test can see that it did not.
    "chain_after": """
        def run(pd, state, **p):
            open(pd["input"]["ran"], "w").write("ran")
            return pd
    """,
    # Waits for a gate file, so a test can look at the board meanwhile.
    "chain_gated": """
        import os, time
        def run(pd, state, **p):
            while not os.path.exists(pd["input"]["gate"]):
                time.sleep(0.02)
            return pd
    """,
    "chain_sleep": """
        import time
        def run(pd, state, **p):
            time.sleep(pd["input"]["sleep"])
            return pd
    """,
    # Notes when it starts and ends, to see whether two ran at once.
    "chain_solo": """
        import time
        def run(pd, state, **p):
            with open(pd["input"]["log"], "a") as f:
                f.write(f"start {time.monotonic()}\\n")
            time.sleep(0.4)
            with open(pd["input"]["log"], "a") as f:
                f.write(f"end {time.monotonic()}\\n")
            return pd
    """,
    "chain_first_a": """
        def run(pd, state, **p):
            return pd
    """,
    "chain_first_b": """
        def run(pd, state, **p):
            return pd
    """,
}


@pytest.fixture(scope="module", autouse=True)
def steps():
    for name, code in STEPS.items():
        write_step(code, name=name)


def register(engine, steps_yaml, **engine_kwargs):
    output = fresh_output()
    engine.register("wf", write_yaml("wf:\n" + steps_yaml), output)
    return output


def record_ends(output):
    """The run record's end lines, by job."""
    lines = (Path(output) / "analysis" / "wf" / "run.jsonl").read_text().splitlines()
    return {line["job"]: line for line in map(json.loads, lines) if line["event"] == "end"}


def test_steps_of_one_environment_write_one_folder(engine_factory):
    engine = engine_factory()
    register(engine, "  - chain_add:\n  - chain_add:\n  - chain_add:\n")
    job = engine.submit("wf", {"n": 0})
    board = engine.status("wf")
    while board[job]["state"] not in ("done", "failed"):
        time.sleep(0.05)
        board = engine.status("wf")

    output = board[job]["output"]
    assert output["n"] == 3
    # One chain of three steps: one folder, named from its first index to its last step.
    assert Path(output.folder).name == "0-chain_add"
    engine.deregister("wf")


def test_a_different_max_workers_starts_a_new_chain(engine_factory):
    engine = engine_factory()
    register(engine, "  - chain_add:\n  - chain_fresh:\n      max_workers: 2\n")
    job = engine.submit("wf", {"n": 1})
    board = engine.status("wf")
    while board[job]["state"] not in ("done", "failed"):
        time.sleep(0.05)
        board = engine.status("wf")

    assert board[job]["output"]["n"] == 20
    assert Path(board[job]["output"].folder).name == "1-chain_fresh"
    engine.deregister("wf")


def test_every_step_of_a_chain_has_its_record_and_provenance(engine_factory):
    engine = engine_factory()
    output = register(engine, "  - chain_add:\n  - chain_fresh:\n")
    job = engine.submit("wf", {"n": 1})
    board = engine.deregister("wf")

    assert set(board[job]["output"]["provenance"]) == {"chain_add", "chain_fresh"}
    steps = record_ends(output)[job]["steps"]
    assert [s["step"] for s in steps] == ["chain_add", "chain_fresh"]
    assert all(s["start"] <= s["end"] for s in steps)
    assert all("packages" in s for s in steps)


def test_the_board_shows_the_step_of_a_chain_that_runs(engine_factory, tmp_path):
    gate = tmp_path / "gate"
    engine = engine_factory()
    register(engine, "  - chain_add:\n  - chain_gated:\n")
    job = engine.submit("wf", {"n": 0, "gate": str(gate)})
    try:
        deadline = time.monotonic() + 30
        while engine.status("wf")[job]["step"] != "chain_gated":
            assert time.monotonic() < deadline, "the board never showed the second step"
            time.sleep(0.05)
        assert engine.status("wf")[job]["state"] == "running"
    finally:
        gate.write_text("open")
    engine.deregister("wf")


def test_a_failure_in_a_chain_names_its_step_and_stops_the_chain(engine_factory, tmp_path):
    ran = tmp_path / "ran"
    engine = engine_factory()
    output = register(engine, "  - chain_add:\n  - chain_break:\n  - chain_after:\n")
    job = engine.submit("wf", {"n": 0, "ran": str(ran)})
    board = engine.deregister("wf")

    assert board[job]["state"] == "failed"
    assert board[job]["step"] == "chain_break"
    assert "broken in the middle" in board[job]["error"]
    assert not ran.exists()
    # The step that finished before it is in the record.
    assert [s["step"] for s in record_ends(output)[job]["steps"]] == ["chain_add"]


def test_each_step_of_a_chain_has_its_own_timeout(engine_factory):
    # A new worker first reads its environment's packages, which counts
    # against the first step; 4 s leaves room for that.
    engine = engine_factory(execution_timeout=4.0)
    register(engine, "  - chain_sleep:\n  - chain_sleep:\n")
    # Two steps of 2.5 s each: 5 s together, but each inside its 4 s.
    job = engine.submit("wf", {"sleep": 2.5})
    board = engine.deregister("wf")

    assert board[job]["state"] == "done"


def test_a_timeout_in_a_chain_names_its_step(engine_factory):
    engine = engine_factory(execution_timeout=4.0)
    register(engine, "  - chain_add:\n  - chain_sleep:\n")
    job = engine.submit("wf", {"sleep": 10.0})
    board = engine.deregister("wf")

    assert board[job]["state"] == "failed"
    assert board[job]["step"] == "chain_sleep"
    assert "timed out" in board[job]["error"]


def test_a_steps_limit_holds_in_two_different_chains(engine_factory, tmp_path):
    """chain_solo (max_workers 1) sits in two chains of two pipelines."""
    log = tmp_path / "log"
    engine = engine_factory(max_concurrent=4)
    engine.register("one", write_yaml("wf:\n  - chain_first_a:\n  - chain_solo:\n"), fresh_output())
    engine.register("two", write_yaml("wf:\n  - chain_first_b:\n  - chain_solo:\n"), fresh_output())
    for name in ("one", "two", "one", "two"):
        engine.submit(name, {"log": str(log)})
    engine.deregister("one")
    engine.deregister("two")

    times = [
        (word, float(t)) for word, t in (line.split() for line in log.read_text().splitlines())
    ]
    assert len(times) == 8
    running = 0
    for word, _ in sorted(times, key=lambda wt: wt[1]):
        running += 1 if word == "start" else -1
        assert running <= 1, "chain_solo ran twice at once"
