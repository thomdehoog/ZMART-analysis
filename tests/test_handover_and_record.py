"""
Environments named, data between steps on disk, and a run record.

A step names its environment. Every pipeline is registered with an output
place, and the engine writes into ``<output>/analysis/<pipeline>/``: a copy
of the pipeline YAML, the run record ``run.jsonl``, and the files handed
from one step to the next. Arrays travel as ``.npy`` files, so the engine
itself never needs NumPy, and what waits for a scope is a folder, not data.

Usage
-----
    python -m pytest tests/test_handover_and_record.py -v
"""

import hashlib
import json
import platform
import socket
import subprocess
import sys
import textwrap
import threading
import time
import tracemalloc
from pathlib import Path

import pytest
from helpers import _write_step as write_step
from helpers import _write_yaml as write_yaml
from helpers import fresh_output

from zmart_analysis import Engine

MAKE_ARRAY = """
    import numpy as np

    def run(pd, state, **p):
        pd["make_array"] = {"image": np.arange(12, dtype=np.uint16).reshape(3, 4) * pd["input"]["k"],
                            "mean": np.float64(2.5)}
        return pd
"""

DOUBLE_IN_PLACE = """
    import numpy as np

    def run(pd, state, **p):
        image = pd["make_array"]["image"]
        image *= 2
        pd["double_in_place"] = {"sum": int(image.sum()), "type": type(image).__name__}
        return pd
"""

PLAIN = """
    def run(pd, state, **p):
        pd["plain"] = {"value": pd["input"]["value"]}
        return pd
"""

FAIL = """
    def run(pd, state, **p):
        raise ValueError("broken on purpose")
"""

# Per image: an 80 kB array whose sum is the image's number times 10,000.
TILE = """
    import numpy as np

    def run(pd, state, **p):
        n = pd["input"]["n"]
        pd["tile"] = {"n": n, "pixels": np.full(10_000, n, dtype=np.float64)}
        return pd
"""

# Over a unit: what it received, in order, and how many arrays were kept.
COUNT = """
    def run(pd, state, **p):
        tiles = [part["output"]["tile"] for part in pd["parts"]]
        sums = [float(tile["pixels"].sum()) for tile in tiles]
        return {"n": [tile["n"] for tile in tiles], "sums": sums,
                "kept": pd["metadata"]["output"]}
"""


# Returns an object of a class only the step's own module knows.
OWN_CLASS = """
    class Own:
        pass

    def run(pd, state, **p):
        pd["own_class"] = {"thing": Own()}
        return pd
"""

# A scoped step that hands its parts on unchanged.
PASS_PARTS = """
    def run(pd, state, **p):
        return pd
"""

MASKED = """
    import numpy as np

    def run(pd, state, **p):
        pd["masked"] = {"values": np.ma.masked_array([1.0, 2.0, 3.0], mask=[0, 1, 0])}
        return pd
"""

SLOW = """
    import time

    def run(pd, state, **p):
        time.sleep(pd["input"]["seconds"])
        pd["slow"] = {"slept": pd["input"]["seconds"]}
        return pd
"""


@pytest.fixture(scope="module", autouse=True)
def steps():
    for name, code in {
        "own_class": OWN_CLASS,
        "pass_parts": PASS_PARTS,
        "masked": MASKED,
        "slow": SLOW,
        "make_array": MAKE_ARRAY,
        "double_in_place": DOUBLE_IN_PLACE,
        "v5_plain": PLAIN,
        "v5_fail": FAIL,
        "v5_tile": TILE,
        "v5_count": COUNT,
    }.items():
        write_step(code, name=name)


def wait_for(engine, name, job, timeout=60):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        entry = engine.status(name)[job]
        if entry["state"] in ("done", "failed", "cancelled"):
            return entry
        time.sleep(0.02)
    raise AssertionError(f"job {job} did not finish: {engine.status(name)[job]}")


def lines(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


# ---- registering -------------------------------------------------------


def test_a_step_without_an_environment_is_refused(engine_factory, tmp_path):
    step = tmp_path / "no_environment.py"
    step.write_text("def run(pd, state, **p):\n    return pd\n")
    pipeline = tmp_path / "p.yaml"
    pipeline.write_text(
        f'metadata:\n  functions_dir: "{tmp_path.as_posix()}"\nwf:\n  - no_environment:\n'
    )
    engine = engine_factory()
    with pytest.raises(ValueError, match="no_environment.*environment"):
        engine.register("p", str(pipeline), fresh_output())


def test_register_needs_an_output(engine_factory):
    engine = engine_factory()
    with pytest.raises(TypeError):
        engine.register("p", write_yaml("wf:\n  - v5_plain:"))


def test_register_writes_the_pipeline_and_its_record(engine_factory):
    pipeline = write_yaml("wf:\n  - v5_plain:")
    output = fresh_output()
    engine = engine_factory()
    engine.register("p", pipeline, output)

    folder = Path(output) / "analysis" / "p"
    assert (folder / "pipeline.yaml").read_text() == Path(pipeline).read_text()
    (register,) = lines(folder / "run.jsonl")
    assert register["event"] == "register"
    assert register["pipeline"] == "p" and register["workflow"] == "wf"
    assert register["yaml_text"] == Path(pipeline).read_text()
    step_file = Path(pipeline).parent / "v5_plain.py"
    assert register["steps"] == {"v5_plain": hashlib.sha256(step_file.read_bytes()).hexdigest()}


# ---- data between steps --------------------------------------------------


def test_an_array_reaches_the_next_step_and_the_board(engine_factory):
    output = fresh_output()
    engine = engine_factory()
    engine.register("p", write_yaml("wf:\n  - make_array:\n  - double_in_place:"), output)

    entry = wait_for(engine, "p", engine.submit(pipeline="p", data={"k": 3}))

    assert entry["state"] == "done"
    out = entry["output"]
    assert out["double_in_place"] == {"sum": 2 * 3 * 66, "type": "ndarray"}
    # Step 2 changed the array in place; the board shows its last state.
    assert out["make_array"]["image"].sum() == 2 * 3 * 66
    assert out["make_array"]["mean"] == 2.5 and type(out["make_array"]["mean"]) is float
    assert out["input"] == {"k": 3}
    assert dict(out)["double_in_place"]["sum"] == 396


def test_the_hand_over_files_are_npy_and_intermediate_ones_go(engine_factory):
    output = fresh_output()
    engine = engine_factory()
    engine.register("p", write_yaml("wf:\n  - make_array:\n  - double_in_place:"), output)
    wait_for(engine, "p", engine.submit(pipeline="p", data={"k": 1}))

    handover = Path(output) / "analysis" / "p" / ".handover"
    kept = sorted(p.relative_to(handover).as_posix() for p in handover.rglob("*") if p.is_file())
    # One registration, one job, one chain of both steps: a single folder,
    # named from the chain's first index to its last step.
    assert len({k.split("/")[0] for k in kept}) == 1
    assert [k.split("/", 1)[1] for k in kept] == [
        "1/0-double_in_place/array_0.npy",
        "1/0-double_in_place/data.pickle",
    ]


def test_a_failed_job_leaves_no_hand_over_files(engine_factory):
    output = fresh_output()
    engine = engine_factory()
    engine.register("p", write_yaml("wf:\n  - make_array:\n  - v5_fail:"), output)
    entry = wait_for(engine, "p", engine.submit(pipeline="p", data={"k": 1}))

    assert entry["state"] == "failed" and "broken on purpose" in entry["error"]
    assert not [
        p for p in (Path(output) / "analysis" / "p" / ".handover").rglob("*") if p.is_file()
    ]


def test_the_engine_never_imports_numpy_to_hand_an_array_on(tmp_path):
    """In a fresh process, since this one has NumPy loaded already."""
    pipeline = write_yaml("wf:\n  - make_array:\n  - double_in_place:")
    script = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
        from zmart_analysis import Engine
        with Engine() as e:
            e.register("p", {pipeline!r}, {str(tmp_path)!r})
            job = e.submit(pipeline="p", data={{"k": 2}})
            while e.status("p")[job]["state"] not in ("done", "failed"):
                time.sleep(0.02)
            state = e.status("p")[job]["state"]
            print(state, "numpy" in sys.modules)
    """)
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
    )
    assert done.stdout.split()[-2:] == ["done", "False"], done.stderr[-2000:]


def test_a_plain_step_never_imports_numpy_in_its_worker(engine_factory):
    write_step(
        """
        import sys

        def run(pd, state, **p):
            pd["probe"] = {"numpy": "numpy" in sys.modules}
            return pd
        """,
        name="v5_probe",
    )
    engine = engine_factory()
    engine.register("p", write_yaml("wf:\n  - v5_plain:\n  - v5_probe:"), fresh_output())
    entry = wait_for(engine, "p", engine.submit(pipeline="p", data={"value": 1}))
    assert entry["output"]["probe"] == {"numpy": False}


# ---- scopes ----------------------------------------------------------------


def test_a_large_scope_keeps_the_engine_memory_flat(engine_factory):
    """1,000 images of 80 kB arrays: 80 MB if the engine held them. What it
    does keep per job (the board, a summary, a folder name) stays far below."""
    engine = engine_factory()
    engine.register(
        "p", write_yaml("wf:\n  - v5_tile:\n  - v5_count:\n      scope: group"), fresh_output()
    )
    tracemalloc.start()
    before = tracemalloc.get_traced_memory()[0]
    for n in range(1000):
        engine.submit(
            pipeline="p", data={"n": n}, scope={"group": 1}, complete="group" if n == 999 else None
        )
    entry = wait_for(engine, "p", 1001, timeout=600)
    grown = tracemalloc.get_traced_memory()[0] - before
    tracemalloc.stop()

    assert entry["state"] == "done", entry
    assert entry["output"]["n"] == list(range(1000))
    assert entry["output"]["sums"] == [10_000.0 * n for n in range(1000)]
    assert grown < 20_000_000, f"the engine grew {grown} bytes"


def test_a_scoped_step_sees_the_output_folder(engine_factory):
    output = fresh_output()
    engine = engine_factory()
    engine.register("p", write_yaml("wf:\n  - v5_tile:\n  - v5_count:\n      scope: group"), output)
    engine.submit(pipeline="p", data={"n": 1}, scope={"group": 1}, complete="group")
    entry = wait_for(engine, "p", 2)
    assert Path(entry["output"]["kept"]) == Path(output) / "analysis" / "p"
    assert entry["output"]["lineage"]["submissions"] == [1]


# ---- the run record ----------------------------------------------------------


def test_the_record_grows_while_the_pipeline_runs(engine_factory):
    output = fresh_output()
    engine = engine_factory()
    engine.register("p", write_yaml("wf:\n  - v5_plain:"), output)
    record = Path(output) / "analysis" / "p" / "run.jsonl"

    job = engine.submit(
        pipeline="p", data={"value": 7, "image_path": "a/b.tif"}, scope={"group": 2}
    )
    wait_for(engine, "p", job)
    events = lines(record)
    wait_for(engine, "p", engine.submit(pipeline="p", data={"value": 8}))
    assert len(lines(record)) == len(events) + 2

    assert [e["event"] for e in events] == ["register", "submit", "end"]
    submit, end = events[1], events[2]
    assert submit["job"] == job and submit["data"] == {"value": 7, "image_path": "a/b.tif"}
    assert submit["scope"] == {"group": 2} and submit["complete"] is None
    assert end["state"] == "done" and end["job"] == job
    assert [s["step"] for s in end["steps"]] == ["v5_plain"]
    assert end["steps"][0]["python"] == platform.python_version()
    assert len(end["steps"][0]["fingerprint"]) == 16
    assert {e["registration"] for e in events} == {events[0]["registration"]}


def test_the_record_has_the_error_and_the_jobs_of_a_scope(engine_factory):
    output = fresh_output()
    engine = engine_factory()
    engine.register("p", write_yaml("wf:\n  - v5_tile:\n  - v5_count:\n      scope: group"), output)
    engine.register("f", write_yaml("wf:\n  - v5_fail:"), output)
    engine.submit(pipeline="p", data={"n": 1}, scope={"group": 1})
    engine.submit(pipeline="p", data={"n": 2}, scope={"group": 1}, complete="group")
    wait_for(engine, "p", 3)
    wait_for(engine, "f", engine.submit(pipeline="f", data={}))

    ends = [e for e in lines(Path(output) / "analysis" / "p" / "run.jsonl") if e["event"] == "end"]
    assert [e["job"] for e in ends if e["jobs"]] == [3]
    assert [e["jobs"] for e in ends if e["jobs"]] == [[1, 2]]
    (failed,) = [
        e for e in lines(Path(output) / "analysis" / "f" / "run.jsonl") if e["event"] == "end"
    ]
    assert failed["state"] == "failed" and "broken on purpose" in failed["error"]


def test_a_directly_submitted_array_is_recorded_by_its_shape(engine_factory):
    import numpy as np

    output = fresh_output()
    engine = engine_factory()
    engine.register("p", write_yaml("wf:\n  - v5_plain:"), output)
    job = engine.submit(pipeline="p", data={"value": 1, "image": np.zeros((5, 6), dtype=np.uint8)})
    wait_for(engine, "p", job)
    (submit,) = [
        e for e in lines(Path(output) / "analysis" / "p" / "run.jsonl") if e["event"] == "submit"
    ]
    assert submit["data"]["image"] == {"array": [5, 6], "dtype": "uint8"}


# ---- several pipelines and registrations ----------------------------------


def test_two_pipelines_in_one_place_keep_apart(engine_factory):
    output = fresh_output()
    engine = engine_factory()
    engine.register("a", write_yaml("wf:\n  - v5_plain:"), output)
    engine.register("b", write_yaml("wf:\n  - v5_plain:"), output)
    wait_for(engine, "b", engine.submit(pipeline="b", data={"value": 1}))

    board = engine.deregister("a")

    assert board == {}
    assert (Path(output) / "analysis" / "b" / "run.jsonl").exists()
    assert wait_for(engine, "b", engine.submit(pipeline="b", data={"value": 2}))["state"] == "done"
    assert [e["event"] for e in lines(Path(output) / "analysis" / "a" / "run.jsonl")] == [
        "register",
        "deregister",
    ]


def test_deregister_returns_a_readable_board_and_cleans_up(engine_factory):
    output = fresh_output()
    engine = engine_factory()
    engine.register("p", write_yaml("wf:\n  - make_array:"), output)
    wait_for(engine, "p", engine.submit(pipeline="p", data={"k": 1}))

    board = engine.deregister("p")

    assert board[1]["output"]["make_array"]["image"].sum() == 66
    assert not list((Path(output) / "analysis" / "p" / ".handover").iterdir())
    last = lines(Path(output) / "analysis" / "p" / "run.jsonl")[-1]
    assert last["event"] == "deregister" and last["done"] == 1 and last["failed"] == 0


def test_an_output_taken_from_the_board_is_readable_after_shutdown():
    with Engine() as engine:
        engine.register("p", write_yaml("wf:\n  - make_array:"), fresh_output())
        entry = wait_for(engine, "p", engine.submit(pipeline="p", data={"k": 2}))

    assert entry["output"]["make_array"]["image"].sum() == 132
    assert entry["output"]["input"] == {"k": 2}


def test_the_same_name_again_adds_to_the_record(engine_factory):
    output = fresh_output()
    engine = engine_factory()
    pipeline = write_yaml("wf:\n  - v5_plain:")
    engine.register("p", pipeline, output)
    wait_for(engine, "p", engine.submit(pipeline="p", data={"value": 1}))
    engine.deregister("p")
    engine.register("p", pipeline, output)
    wait_for(engine, "p", engine.submit(pipeline="p", data={"value": 2}))

    events = lines(Path(output) / "analysis" / "p" / "run.jsonl")
    registers = [e for e in events if e["event"] == "register"]
    assert len(registers) == 2 and registers[0]["registration"] != registers[1]["registration"]
    assert [e["job"] for e in events if e["event"] == "submit"] == [1, 1]


# ---- what the review found ---------------------------------------------------


def test_a_second_registration_of_a_name_leaves_a_live_one_alone(engine_factory):
    """Another engine registers the same name into the same place while the
    first still runs a job: neither touches the other's files."""
    output = fresh_output()
    pipeline = write_yaml("wf:\n  - slow:")
    first, second = engine_factory(), engine_factory()
    first.register("p", pipeline, output)
    finished = first.submit(pipeline="p", data={"seconds": 0})
    wait_for(first, "p", finished)
    running = first.submit(pipeline="p", data={"seconds": 2})
    time.sleep(0.5)

    second.register("p", pipeline, output)
    entry = wait_for(first, "p", running)
    board = first.deregister("p")

    assert entry["state"] == "done" and board[running]["output"]["slow"] == {"slept": 2}
    assert board[finished]["output"]["slow"] == {"slept": 0}
    events = [e["event"] for e in lines(Path(output) / "analysis" / "p" / "run.jsonl")]
    assert events.count("register") == 2 and events.count("deregister") == 1
    assert (
        wait_for(second, "p", second.submit(pipeline="p", data={"seconds": 0}))["state"] == "done"
    )


def test_a_leftover_of_a_dead_registration_is_cleared(engine_factory):
    output = fresh_output()
    handover = Path(output) / "analysis" / "p" / ".handover"
    dead = handover / f"20200101-000000-000000-999999@{socket.gethostname()}"
    elsewhere = handover / "20200101-000000-000000-999999@ANOTHER-PC"
    (dead / "1").mkdir(parents=True)
    (elsewhere / "1").mkdir(parents=True)
    engine_factory().register("p", write_yaml("wf:\n  - v5_plain:"), output)
    assert not dead.exists()
    assert elsewhere.exists()  # another computer's registration may still run


def test_a_registration_that_fails_leaves_nothing_behind(engine_factory):
    from zmart_analysis.board import LIVE

    output = fresh_output()
    (Path(output) / "analysis" / "p" / "pipeline.yaml").mkdir(parents=True)  # cannot be written
    live = set(LIVE)
    with pytest.raises(OSError):
        engine_factory().register("p", write_yaml("wf:\n  - v5_plain:"), output)
    assert LIVE == live
    assert not list((Path(output) / "analysis" / "p" / ".handover").iterdir())


def test_an_output_the_engine_cannot_read_does_not_break_deregister(engine_factory):
    output = fresh_output()
    engine = engine_factory()
    engine.register("p", write_yaml("wf:\n  - own_class:"), output)
    job = engine.submit(pipeline="p", data={})
    assert wait_for(engine, "p", job)["state"] == "done"

    board = engine.deregister("p")

    assert board[job]["output"] is None
    assert "own_class" in board[job]["error"] or "Own" in board[job]["error"]
    assert lines(Path(output) / "analysis" / "p" / "run.jsonl")[-1]["event"] == "deregister"
    assert not list((Path(output) / "analysis" / "p" / ".handover").iterdir())


def test_parts_handed_on_by_a_scoped_step_stay_readable(engine_factory):
    engine = engine_factory()
    engine.register(
        "p", write_yaml("wf:\n  - v5_tile:\n  - pass_parts:\n      scope: group"), fresh_output()
    )
    engine.submit(pipeline="p", data={"n": 4}, scope={"group": 1}, complete="group")
    entry = wait_for(engine, "p", 2)
    engine.deregister("p")

    parts = entry["output"]["parts"]
    assert isinstance(parts, list) and parts[0]["output"]["tile"]["pixels"].sum() == 40_000


def test_a_masked_array_reaches_the_board_as_it_was(engine_factory):
    engine = engine_factory()
    engine.register("p", write_yaml("wf:\n  - masked:"), fresh_output())
    entry = wait_for(engine, "p", engine.submit(pipeline="p", data={}))
    values = entry["output"]["masked"]["values"]
    assert values.sum() == 4.0 and list(values.mask) == [False, True, False]


def test_a_record_that_cannot_be_written_does_not_stop_the_jobs(engine_factory):
    output = fresh_output()
    engine = engine_factory()
    engine.register("p", write_yaml("wf:\n  - v5_plain:"), output)
    record = Path(output) / "analysis" / "p" / "run.jsonl"
    record.unlink()
    record.mkdir()  # every write now fails

    entry = wait_for(engine, "p", engine.submit(pipeline="p", data={"value": 3}))

    assert entry["state"] == "done" and entry["output"]["plain"] == {"value": 3}
    engine.deregister("p")


def test_status_and_deregister_at_the_same_time(engine_factory):
    engine = engine_factory()
    engine.register("p", write_yaml("wf:\n  - make_array:"), fresh_output())
    for k in range(5):
        wait_for(engine, "p", engine.submit(pipeline="p", data={"k": k}))
    seen = []

    def poll():
        for _ in range(200):
            try:
                seen.append(engine.status("p"))
            except KeyError:
                return

    poller = threading.Thread(target=poll)
    poller.start()
    engine.deregister("p")
    poller.join()
    for board in seen:
        for job, entry in board.items():
            assert entry["output"]["make_array"]["image"].sum() == 66 * (job - 1)


def test_status_of_every_pipeline_while_one_is_closing(engine_factory):
    engine = engine_factory()
    engine.register("a", write_yaml("wf:\n  - make_array:"), fresh_output())
    engine.register("b", write_yaml("wf:\n  - v5_plain:"), fresh_output())
    wait_for(engine, "a", engine.submit(pipeline="a", data={"k": 1}))
    errors = []

    def poll():
        for _ in range(300):
            try:
                assert "b" in engine.status()
            except Exception as error:  # noqa: BLE001 -- collected for the assert below
                errors.append(error)

    poller = threading.Thread(target=poll)
    poller.start()
    engine.deregister("a")
    poller.join()
    assert errors == []
