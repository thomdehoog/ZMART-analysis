"""
Engine v5: environments named, data between steps on disk, and a run record.

A step names its environment. Every pipeline is registered with an output
place, and the engine writes into ``<output>/analysis/<pipeline>/``: a copy
of the pipeline YAML, the run record ``run.jsonl``, and the files handed
from one step to the next. Arrays travel as ``.npy`` files, so the engine
itself never needs NumPy, and what waits for a scope is a folder, not data.

Usage
-----
    python -m pytest tests/test_v5.py -v
"""

import json
import subprocess
import sys
import textwrap
import time
import tracemalloc
from pathlib import Path

import pytest
from helpers import _write_step as write_step
from helpers import _write_yaml as write_yaml
from helpers import fresh_output

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

# Per image: a small array whose sum is the image's number times 1000.
TILE = """
    import numpy as np

    def run(pd, state, **p):
        n = pd["input"]["n"]
        pd["tile"] = {"n": n, "pixels": np.full(1000, n, dtype=np.float64)}
        return pd
"""

# Over a unit: what it received, in order, and how many arrays were kept.
COUNT = """
    def run(pd, state, **p):
        sums = [float(r["tile"]["pixels"].sum()) for r in pd["results"]]
        return {"n": [r["tile"]["n"] for r in pd["results"]], "sums": sums,
                "kept": pd["metadata"]["output"]}
"""


@pytest.fixture(scope="module", autouse=True)
def steps():
    for name, code in {
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
    assert list(register["steps"]) == ["v5_plain"]
    assert len(register["steps"]["v5_plain"]) == 64


# ---- data between steps --------------------------------------------------


def test_an_array_reaches_the_next_step_and_the_board(engine_factory):
    output = fresh_output()
    engine = engine_factory()
    engine.register("p", write_yaml("wf:\n  - make_array:\n  - double_in_place:"), output)

    entry = wait_for(engine, "p", engine.submit(pipeline="p", data={"k": 3}))

    assert entry["state"] == "done"
    out = entry["output"]
    assert out["double_in_place"] == {"sum": 2 * 3 * 66, "type": "ndarray"}
    # The in-place change in step 2 did not change what step 1 wrote.
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
    # One registration, one job, only its last step's folder left.
    assert len({k.split("/")[0] for k in kept}) == 1
    assert [k.split("/", 1)[1] for k in kept] == [
        "1/1-double_in_place/array_0.npy",
        "1/1-double_in_place/data.pickle",
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
    probe = write_step(
        """
        import sys

        def run(pd, state, **p):
            pd["probe"] = {"numpy": "numpy" in sys.modules}
            return pd
        """,
        name="v5_probe",
    )
    assert probe
    engine = engine_factory()
    engine.register("p", write_yaml("wf:\n  - v5_plain:\n  - v5_probe:"), fresh_output())
    entry = wait_for(engine, "p", engine.submit(pipeline="p", data={"value": 1}))
    assert entry["output"]["probe"] == {"numpy": False}


# ---- scopes ----------------------------------------------------------------


def test_a_large_scope_keeps_the_engine_memory_flat(engine_factory):
    """1,000 images of 8 kB arrays, about 8 MB in total if the engine held them."""
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
    assert entry["output"]["sums"] == [1000.0 * n for n in range(1000)]
    assert grown < 4_000_000, f"the engine grew {grown} bytes"


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

    assert [e["event"] for e in events] == ["register", "submit", "end"]
    submit, end = events[1], events[2]
    assert submit["job"] == job and submit["data"] == {"value": 7, "image_path": "a/b.tif"}
    assert submit["scope"] == {"group": 2} and submit["complete"] is None
    assert end["state"] == "done" and end["job"] == job
    assert [s["step"] for s in end["steps"]] == ["v5_plain"]
    assert end["steps"][0]["python"] and end["steps"][0]["fingerprint"]
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
