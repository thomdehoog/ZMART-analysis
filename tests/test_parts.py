"""
What a scoped step receives: parts.

A scoped step gets ``pipeline_data["parts"]``: one part per image of its
unit, or per narrower unit for a wider scope. Every part has the same
outside, whatever made it:

    {"id": <the job, as on the board>, "scope": <where it is from>,
     "output": <what the steps left, without the engine's own keys>}

and an image part also carries ``input``, what was submitted. The engine
fixes this outside; what is inside ``output`` is the step's own.

The steps run on the engine's own Python, so no conda environment is needed.
"""

import pytest
from helpers import _write_step as write_step
from helpers import _write_yaml as write_yaml
from helpers import fresh_output

#: A per-image step that keeps the whole dictionary, as the shipped steps do.
KEEP = """
    def run(pd, state, **p):
        pd["measure"] = {"value": pd["input"]["value"]}
        return pd
"""

#: A per-image step that hands on a new dictionary, without input or metadata.
FRESH = """
    def run(pd, state, **p):
        return {"measure": {"value": pd["input"]["value"]}}
"""

#: A per-image step that fails on a value of 0.
PICKY = """
    def run(pd, state, **p):
        if pd["input"]["value"] == 0:
            raise ValueError("zero")
        pd["measure"] = {"value": pd["input"]["value"]}
        return pd
"""

#: A scoped step that hands back what it was given, for the test to look at.
SHOW = """
    def run(pd, state, **p):
        parts = list(pd["parts"])
        return {
            "parts": [
                {"id": part["id"], "scope": part["scope"], "output": part["output"],
                 "input": part.get("input")}
                for part in parts
            ],
            "failures": pd["failures"],
        }
"""

#: A scoped step that sums the values of its parts.
SUM = """
    def run(pd, state, **p):
        return {"total": sum(part["output"]["measure"]["value"] for part in pd["parts"])}
"""

#: The second step of a scoped phase: it gets the first's output, as today.
DOUBLE = """
    def run(pd, state, **p):
        return {"twice": pd["total"] * 2}
"""

#: A carrier step that shows the compartments it gets.
SHOW_UNITS = """
    def run(pd, state, **p):
        return {
            "parts": [
                {"id": part["id"], "scope": part["scope"], "output": part["output"]}
                for part in pd["parts"]
            ],
        }
"""

#: A scoped step that fails.
BREAK = """
    def run(pd, state, **p):
        raise RuntimeError("broken unit")
"""


@pytest.fixture(scope="module", autouse=True)
def steps():
    for name, code in {
        "parts_keep": KEEP,
        "parts_fresh": FRESH,
        "parts_picky": PICKY,
        "parts_show": SHOW,
        "parts_sum": SUM,
        "parts_double": DOUBLE,
        "parts_show_units": SHOW_UNITS,
        "parts_break": BREAK,
    }.items():
        write_step(code, name=name)


def run_group(engine, image_step, scoped_steps, values):
    """Submit one image per value into group 1, the last one completing it."""
    lines = "".join(f"  - {name}:\n" for name in scoped_steps[1:])
    pipeline = write_yaml(
        f"wf:\n  - {image_step}:\n  - {scoped_steps[0]}:\n      scope: group\n{lines}"
    )
    engine.register("wf", pipeline, fresh_output())
    for i, value in enumerate(values):
        last = i == len(values) - 1
        engine.submit(
            pipeline="wf",
            data={"value": value},
            scope={"group": 1, "position": i},
            complete="group" if last else None,
        )
    return engine.deregister("wf")


def scope_job(board):
    """The one scope job on a board."""
    (job,) = [job for job, entry in board.items() if entry["scope"] == {"group": 1}]
    return job


def test_an_image_part_has_its_job_scope_output_and_input(engine_factory):
    engine = engine_factory()
    board = run_group(engine, "parts_keep", ["parts_show"], [5, 7])
    shown = board[scope_job(board)]["output"]["parts"]

    assert [part["id"] for part in shown] == [1, 2]
    assert [part["scope"] for part in shown] == [
        {"group": 1, "position": 0},
        {"group": 1, "position": 1},
    ]
    assert [part["output"] for part in shown] == [
        {"measure": {"value": 5}},
        {"measure": {"value": 7}},
    ]
    assert [part["input"] for part in shown] == [{"value": 5}, {"value": 7}]


def test_a_part_knows_its_job_when_the_step_returned_a_new_dictionary(engine_factory):
    engine = engine_factory()
    board = run_group(engine, "parts_fresh", ["parts_show"], [5, 7])
    shown = board[scope_job(board)]["output"]["parts"]

    assert [part["id"] for part in shown] == [1, 2]
    assert shown[0]["scope"] == {"group": 1, "position": 0}
    assert shown[0]["output"] == {"measure": {"value": 5}}
    assert shown[0]["input"] is None


def test_the_steps_after_the_first_in_a_scoped_phase_get_the_step_before(engine_factory):
    engine = engine_factory()
    board = run_group(engine, "parts_keep", ["parts_sum", "parts_double"], [5, 7])

    assert board[scope_job(board)]["output"]["twice"] == 24


def test_a_unit_part_is_the_scope_job_with_its_unit(engine_factory):
    pipeline = write_yaml(
        """
        metadata:
          levels: [carrier, compartment]
        wf:
          - parts_keep:
          - parts_sum:
              scope: compartment
          - parts_show_units:
              scope: carrier
        """
    )
    engine = engine_factory()
    engine.register("wf", pipeline, fresh_output())
    engine.submit("wf", {"value": 1}, scope={"carrier": 1, "compartment": 1})
    engine.submit(
        "wf", {"value": 2}, scope={"carrier": 1, "compartment": 1}, complete="compartment"
    )
    engine.submit("wf", {"value": 3}, scope={"carrier": 1, "compartment": 2})
    engine.submit(
        "wf",
        {"value": 4},
        scope={"carrier": 1, "compartment": 2},
        complete=["compartment", "carrier"],
    )
    board = engine.deregister("wf")

    compartments = {
        tuple(entry["scope"].items()): job
        for job, entry in board.items()
        if "compartment" in entry["scope"] and "parts_sum" == entry["step"]
    }
    (carrier,) = [job for job, entry in board.items() if entry["scope"] == {"carrier": 1}]
    shown = board[carrier]["output"]["parts"]

    assert [part["scope"] for part in shown] == [
        {"carrier": 1, "compartment": 1},
        {"carrier": 1, "compartment": 2},
    ]
    assert [part["id"] for part in shown] == [
        compartments[(("carrier", 1), ("compartment", 1))],
        compartments[(("carrier", 1), ("compartment", 2))],
    ]
    assert [part["output"] for part in shown] == [{"total": 3}, {"total": 7}]
    # The carrier's lineage still lists every image under it.
    assert board[carrier]["output"]["lineage"]["submissions"] == [1, 2, 4, 5]


def test_a_failure_names_the_job_that_failed(engine_factory):
    engine = engine_factory()
    board = run_group(engine, "parts_picky", ["parts_show"], [5, 0, 7])
    output = board[scope_job(board)]["output"]

    assert [part["id"] for part in output["parts"]] == [1, 3]
    (failure,) = output["failures"]
    assert failure["id"] == 2
    assert failure["scope"] == {"group": 1, "position": 1}
    assert failure["step"] == "parts_picky"
    assert "zero" in failure["error"]
    assert set(failure) == {"id", "scope", "step", "error"}
    assert output["lineage"]["failed"] == [failure]


def test_a_failed_scope_job_is_named_by_its_own_id(engine_factory):
    pipeline = write_yaml(
        """
        metadata:
          levels: [carrier, compartment]
        wf:
          - parts_keep:
          - parts_break:
              scope: compartment
          - parts_show:
              scope: carrier
        """
    )
    engine = engine_factory()
    engine.register("wf", pipeline, fresh_output())
    engine.submit(
        "wf",
        {"value": 1},
        scope={"carrier": 1, "compartment": 1},
        complete=["compartment", "carrier"],
    )
    board = engine.deregister("wf")

    (broken,) = [job for job, entry in board.items() if entry["state"] == "failed"]
    (carrier,) = [job for job, entry in board.items() if entry["scope"] == {"carrier": 1}]
    (failure,) = board[carrier]["output"]["failures"]
    assert failure["id"] == broken
    assert failure["scope"] == {"carrier": 1, "compartment": 1}
    assert failure["step"] == "parts_break"


@pytest.mark.parametrize("name", ["input", "metadata", "provenance", "lineage", "parts"])
def test_a_step_named_after_an_engine_key_is_refused(engine_factory, name):
    write_step(KEEP, name=name)
    # A metadata block of its own, so the helper's header lands there and
    # not on a step called "metadata".
    pipeline = write_yaml(f"metadata:\n  verbose: 0\nwf:\n  - {name}:\n")
    engine = engine_factory()
    with pytest.raises(ValueError, match="engine"):
        engine.register("wf", pipeline, fresh_output())
