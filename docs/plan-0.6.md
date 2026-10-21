# ZMART Analysis 0.6 — scopes over time, parts, and a cheaper hand-over

Status: PLAN, not built. Written 2026-10-21 from the engine at 0.5.0rc7
(ZMB `release-candidate`, 8926aa6). Decisions D1–D6 settled with Thom the
same day. Revised after review A (12 findings) and review B (14 findings),
all taken in; see the end.

## Goal

Today a step can look over place: one image, a group, a compartment, a
carrier, the experiment. 0.6 lets a step also look over time: the same
place at this timepoint, or at this one and the ones before it. Because
the microscope waits for these answers, a submit says by when it needs
them. And what a scoped step receives gets one clear shape, the **part**.
And data moves between steps with less reading and writing.

The first caller is **tracking cells per position**: a step that sees a
position at this timepoint and the one before, keeps each cell's id, and
must answer before the microscope comes back to that position.

A pipeline or script that uses no time behaves as in 0.5, with two
exceptions, both breaking and both in the CHANGELOG: `priority` is
replaced by `deadline` (D3), and a scoped step reads `parts` instead of
`results` (D5). Nothing shipped uses either: no workflow in `workflows/`
has a scoped step, and the interface never passes `scope` or `priority`.

## The words

- An image's **scope** gets one more key, `timepoint`, a whole number the
  acquisition gives: `{"carrier": 1, "compartment": 3, "position": 7, "timepoint": 4}`.
  The engine never counts timepoints itself.
- A scoped step gets one more key in the pipeline, **`timepoints`**: how
  many timepoints it looks at. `1` (the default) or a number `N`.
- A submit's **`deadline`**: seconds from now by which the microscope needs
  this image's answer. It replaces `priority`.
- A scoped step receives **parts**: each says which job made it (`id`),
  where and when it is from (`scope`), and what was made (`output`).

Place is a nesting (carrier › compartment › group › position). Time is a
line beside it. A step chooses a block: how much place (`scope`) and how
much time (`timepoints`).

## What a pipeline looks like

```yaml
metadata:
  levels: [experiment, carrier, compartment, group, position]

tracking:
  - segment:                    # every image, as soon as it arrives
  - track_cells:
      scope: position
      timepoints: 2             # this timepoint and the one before
```

And per round, for a step that looks at a well at one timepoint:

```yaml
  - count_per_well:
      scope: compartment        # timepoints: 1 is the default
```

The rule for phases stays as today: the steps after a scoped step, up to
the next `scope`, run with it on the same unit. A scoped phase works on
the outputs of the phase before it. `timepoints` belongs to the phase:
it is set on the phase's first step, and the steps after it in the phase
receive the step before's output, as today.

## Behaviour over time

### The unit

The unit of a scoped phase is its place at a timepoint:

| `timepoints` | The unit | Example |
|---|---|---|
| `1` | place + this timepoint | compartment 3 of carrier 1 at timepoint 4 |
| `N` | place + this timepoint, with the N−1 before it | position 7 of compartment 3 of carrier 1 at 4, with 3 |

- The place is the unit's level **and every wider level the scope
  names**, as `scope_key` does today. `position` must therefore be in the
  pipeline's `levels` when a step has `scope: position`, so that position
  7 of well 3 and position 7 of well 4 stay apart. Register refuses
  `scope: position` without it.
- Units with the same place at different timepoints stay apart.
- A step over everything (`scope: experiment`, or a level the images do
  not name) with `timepoints: 1` is one unit **per timepoint** when the
  images have one: `{"timepoint": 4}`. Without `timepoint` it is the one
  unit of 0.5.
- An image without `timepoint` in its scope is at no timepoint, and its
  units are the units of 0.5.

### When it runs

The engine never guesses; the acquisition says what is complete.

- A phase runs when its place at that timepoint is complete:
  `complete="compartment"` with `timepoint: 4` in the scope closes
  compartment 3 at timepoint 4.
- **`scope: position`** needs no `complete`: one position at one timepoint
  is one image, complete when its image's phase 0 is done.
- **`complete="timepoint"`** closes the whole round: every open unit of
  that timepoint, narrowest first, including the per-timepoint unit of a
  step over everything.
- `complete="experiment"` closes everything still open, as today.
- **A late image** of timepoint 3 reopens its unit at timepoint 3, as
  today; the next complete that covers it closes it again and its phase
  runs again on the new images only (today's rule). A window step that
  already ran at timepoint 4 is **not** run again: its answer stands, and
  the board shows the rerun of timepoint 3 as a new scope job. (Stated
  in the guide as a limit, not hidden.)

### A phase over N timepoints

- Its first step receives its parts grouped by timepoint, oldest first:
  `pipeline_data["parts"] = [{"timepoint": 3, "parts": [...]}, {"timepoint": 4, "parts": [...]}]`.
  A timepoint the place never had (the first timepoints of a series, a
  skipped round) is missing from the list, not empty.
- **`previous`:** the first step also receives `pipeline_data["previous"]`:
  the output of this phase (its last step) for the same place at the
  timepoint before, read from its folder; `None` at the first timepoint,
  or when that run failed (its failure is in `failures`).
- **In order per place, without blocking:** the phase of position 7 at
  timepoint 4 is started only when the phase at its timepoint before has
  ended (done or failed). The engine does not park a thread to wait: when
  timepoint 3 ends, it starts timepoint 4 if that one is ready, and when
  timepoint 4 becomes ready first, it is held as "waiting for timepoint
  3" on the board. No thread waits on another, so `max_concurrent`
  cannot fill up with waiters.
- **What is kept:** the engine keeps the references to the phase-before's
  results of a place for its last N−1 timepoints, instead of taking them
  once. They are small (a folder name, an id, a scope). The folders
  themselves stay until deregister, as every board output does today
  (the board keeps every output; see "Out of scope").

### Deadline

- `submit(..., deadline=90)`: the microscope needs this image's answer
  within 90 seconds. The engine stores it as an absolute time.
- **Order:** the job with the nearest deadline starts first; a job without
  a deadline comes after those with one; ties go by submission order.
- The order holds everywhere a job waits:
  - for an engine thread (the image jobs' queue);
  - **for a scope job's thread** (today a plain first-come
    `ThreadPoolExecutor`; it gets the same ordered queue);
  - **for the step's worker** (`max_workers`; today a plain semaphore).
- A scope job takes the nearest deadline of the images that completed it.
- A window phase held for its timepoint before keeps its own deadline;
  give every timepoint a deadline (the guide says so), and timepoint 3 is
  ahead of timepoint 4 anyway.
- On the board a job with a deadline shows `deadline` (the time) and, once
  ended, `late: True` if it ended after it. The run record writes both.
  The engine does not cancel a late job; the acquisition decides what a
  late answer is worth.
- `priority` is removed from `submit`. Passing it raises `TypeError` with a
  message that names `deadline`.

## In and output: the part

### Why

Thom: "we don't want to be restrictive for users, but at the same time,
we also don't want to be confusing to users." The rule: **the engine fixes
the envelope, the user owns the contents.** The engine never asks what a
step computed, only who computed it and where.

Today a scoped step gets `results`: each entry is whatever dictionary the
step before stored, and what it is and where it is from has to be dug
out of it: an image's `metadata["submission_idx"]` and `metadata["scope"]`
(only there because the shipped steps return the whole `pipeline_data`),
a unit's `metadata["unit"]`, and for a unit no job id at all. The board
calls the id "job"; inside a step it is `submission_idx`.

### The part

```python
part = {
    "id": 12,                                         # the job id, as on the board
    "scope": {"compartment": 3, "position": 7, "timepoint": 4},
    "output": {"segment": {...}, "measure": {...}},   # what the steps left
}
```

- **`id`**: the job that made it, the same number as on the board. For an
  image part the image job; for a unit part the scope job.
- **`scope`**: for an image part the submit's scope; for a unit part the
  unit (as the board shows a scope job's scope).
- **`output`**: what the steps left, minus the engine's own keys
  (`input`, `metadata`, `provenance`, `lineage`). The engine never looks
  inside. The guide recommends, as today, that a step puts its output
  under its own name; it is a convention, not a rule
  (`build_object_table` writes `object_analysis`).
- **`input`**, on an image part only, when the per-image steps kept it:
  what was submitted (the image path, the stage position). A tracker
  needs it, and only an image has it.

The engine builds parts **at read time**; it does not wrap anything when
it stores. What a step returns is stored exactly as today, so the
hand-over files, the board and the run record keep their 0.5 shape.

### Where id and scope come from

They do not come from the stored dictionaries (a per-image step that
returns a new dictionary drops `metadata`; a unit's stored metadata has
no job id). The engine knows them and hands them over:

- **Order and identity are kept apart.** Every collect, hold and wait
  today filters by "submitted up to the closing image" (`idx <= before`),
  and the scope jobs of a submit are numbered after its image. So the
  number used for that order stays the closing submit's index, and the
  part's identity is stored beside it: each stored result is
  `(order, match_scope, (folder, id, part_scope))`. For an image `id` is
  its job and `part_scope` its scope; for a unit `id` is the **scope
  job** and `part_scope` the **unit**. (Storing the scope job's id as the
  order would make a well drop out of its own carrier when one submit
  completes both.)
- Failures likewise keep `submission_idx` for the order and add `id` for
  the part.
- `files["results"]` becomes a list of `(folder, id, part_scope)`, and the
  worker's `parts` sequence reads each folder when the step reaches it and
  builds the part. A scoped step that hands its input on in what it
  returns is stored with the same triples (the `RESULTS` marker in
  `handover.py`) and reads back as parts on the board.

### What a step receives and returns

- **A per-image step does not change.**
- **The first step of a scoped phase** gets `pipeline_data["parts"]`
  (instead of `results`), ordered by id, read one part at a time as
  `results` is today; and `pipeline_data["failures"]`.
- **The steps after it in the phase** receive the step before's output,
  as today.
- **A scoped step returns a new dictionary**, as guide 2 already says.
  Anything inside it is free. The engine adds `metadata`, `provenance`
  and `lineage` to what is stored, as today; they are not part of the
  next part's `output`.
- **Failures** get the same outside: `{"id", "scope", "step", "error"}`,
  where `id` is the job that failed as on the board (a failed scope job
  is the scope job, not the image that closed it) and `scope` is the
  image's scope or the unit. A note from the engine (a narrower unit
  never closed) has `"step": "engine"` and the id of the job it is about.
  `record_failure` and `held_for` take the job id for this.
- `lineage["failed"]` on the board takes the same shape (it is the
  failures, kept), which is the one change the board sees. Its
  `submissions` stay.
- **Step names the engine uses are refused at register:** `input`,
  `metadata`, `provenance`, `lineage`, `parts`, `failures`, `previous`.
  A step of that name would overwrite the engine's key. A key of those
  names inside what a step returns is the engine's: it is not part of the
  next part's `output`.
- The one check stays: a step that returns anything but a dictionary
  fails with a message that says a dictionary was expected.

### What a scoped step no longer sees

The `provenance` of the steps before it (today inside each result). It
stays on the board and in `lineage["provenance"]`, which is where it is
read; a step does not need it to do its work. (`test_scopes.py`'s
`inputs_provenance` test is rewritten to read the lineage.)

## Hand-over: memory inside a worker, local disk between workers

### Why

Thom: "read write can be very costly." Today every step writes its whole
output (arrays as `.npy`, the rest pickled) and the next step reads it
back, also when both run in the same environment. And the files go to
`<run folder>/analysis/<pipeline>/.handover/`, inside the acquisition's
own data folder: on a microscope that is the data disk or a network
share, busy with the images themselves. A 100-plane, 4-channel z-stack is
about 3.2 GB in uint16; a four-step pipeline writes and reads it again at
every step.

The rule: **memory inside a worker, a disk of your choice between
workers, the run folder only for what is kept.** It is not decided by
measuring; the gain is measured after it is built, to state it in the
CHANGELOG.

### Steps that run together: a chain

- **A chain** is a run of consecutive steps in one phase whose resolved
  settings (the step file's environment and `max_workers`, after the
  pipeline's override) are the same. Chains are built in `register`,
  where those settings are known, per occurrence of a step in the
  pipeline (a step listed twice may sit in two different chains).
- A chain is **one call to one worker**: the worker runs its steps in
  order and passes the dictionary from one to the next in memory. Every
  middle step's output still goes through `finish` (the dictionary check,
  the phase's `metadata` for the next step), without the write; `lineage`
  is added to the last output only. Only the chain's last output is
  written.
- **`max_workers` counts chains.** The wait for a worker is keyed by the
  chain (its step files and its width), so a chain's limit holds across
  pipelines as a step's does today. Guide 2 says so.
- **The trade, stated:** steps with `max_workers: 1` that used to overlap
  (job 2's `read_images` while job 1's `score_focus` runs) now run as one
  chain per job. Each job writes less; a batch of jobs at once can be
  slower when a step's own limit was what kept the overlap. The
  measurement after the build includes a batch, not only one job.
- The shipped pipelines:
  - `focus`: `read_images → score_focus` → one chain (one write instead of
    two);
  - `object_analysis_watershed`: all four steps, classical, `max_workers:
    12` → one chain (one write instead of four);
  - `object_analysis_cellpose`: `[read_images] → [segment_cellpose] →
    [measure_objects, build_object_table]` → three writes instead of four;
    the hand-overs left are the ones between environments.

### What the worker tells the engine during a chain

- Before each step of a chain the worker sends a short message, "at step
  X", and after it the step's record: `{step, start, end, provenance}`.
  The answer at the end carries the last output and the list of records;
  an error answer carries the records so far and the failing step's name.
- The engine:
  - marks the board's `step` only (the job stays `running`; `job_at`,
    which sets `queued`, is not reused for this);
  - restarts the step timeout at each "at step X": `execution_timeout`
    is a real per-step limit, also inside a chain, and `None` means none,
    as today;
  - remembers the last step it was told about, so `StepExecutionError`,
    `WorkerTimeoutError` and `WorkerCrashedError` carry `.step`, and the
    board's failed job names the right step even after a crash or
    timeout;
  - writes the records into the run record per step, as today, also for
    the steps of a failed chain that did finish.
- Provenance per step comes from these records, not from the output
  dictionary, so a step that returns a new dictionary cannot drop the
  provenance of the steps before it.
- `answer_arrives` (the macOS-safe wait) reads these messages in its loop;
  a kill in the middle of a chain still releases the caller with
  `WorkerCrashedError`, and the steps after it never run.

### Between chained steps, values pass as they are

Written to a folder, a NumPy scalar comes back as a Python number and an
array comes back as a fresh copy; passed in memory, neither happens. So:

- An output must still be writable (it is written at the end of its
  chain, and on the board); between chained steps it is passed as it is.
  Guide 2 says both.
- The test "outputs identical with and without chaining" is made on the
  shipped pipelines and the sample images, not promised for every
  pipeline.

### Where the files go

- **`Engine(handover=<folder>)`.** Without it, the hand-over stays where
  it is today, in the run folder, so nothing moves into a Windows profile
  with a size limit by surprise. With it, the hand-over goes to that
  folder: a local data disk on a microscope PC.
- Inside it, always a folder of the engine's own:
  `<handover>/zmart-analysis/<user>/<pipeline>/<registration>/<job>/<step>`.
  Clean-up looks only there, and only removes names that read as a
  registration (`<time>-<pid>@<computer>`); a name it cannot read is left
  alone (today's `leftover` returns "leftover" for those, which in a
  shared folder would remove other things). A process it may not look at
  (another user's) counts as alive.
- The run folder keeps what is meant to stay: `pipeline.yaml`,
  `run.jsonl`, and the files steps write to `metadata["output"]`. The
  checkpoints and masks the interface reads are written by the steps to
  `<run>/analysis/tiles` (`output_dir`), not to the hand-over, and do not
  move.
- **The interface** passes `handover=` (its one change in 0.6): a local
  data disk on the microscope PC, set where it creates the engine.
- **`score_focus` stops handing on the stack.** Its output is the focus
  result; the stack it scored is dropped from what it returns, as
  `build_object_table` already drops its heavy data. Today every focus
  job's final output holds its whole stack until deregister, and the
  interface does not deregister during a session.

### Tests for the hand-over

- a chain of two same-environment steps writes one folder, not two;
- steps with different resolved environments or `max_workers` (including
  a pipeline override) are not chained; a step listed twice is placed per
  occurrence;
- `max_workers: 1` on a chain holds across two pipelines using it;
- the board shows each step of a chain in turn, with the job `running`;
- a failure in the middle of a chain names that step on the board and in
  the error; the steps after it do not run; the steps before it are in
  the run record;
- a timeout in the middle of a chain names that step; each step has its
  own timeout;
- provenance has an entry for every step of a chain, also when a step
  returns a new dictionary;
- with `handover=` nothing is written under the run folder's
  `analysis/<pipeline>/` except `pipeline.yaml`, `run.jsonl` and what
  steps keep; without it, the files are where they are today;
- clean-up removes a crashed run's registration folder and nothing else
  (a foreign folder in the hand-over folder survives);
- the shipped pipelines give the same outputs with and without chaining
  on the sample images;
- `score_focus`'s output no longer holds the stack.

Measured after the build, to report: the watershed and Cellpose pipelines
on a 2048×2048 tile and on a 100-plane, 4-channel stack, one job and a
batch, before and after, with the hand-over in the run folder and on a
local disk.

## How it is built

Thom, 2026-10-21: "emphasis on not over engineering, simple code, no
redundancy, clean fundamental fixes. Not too many abstractions. The right
split between files: not super short, not super long. It should be clear
immediately from the outside how everything clicks together."

### Rules for the build

- **One way to do each thing.** One order (deadline, then id), in two
  small pieces built on it: an ordered thread pool (image jobs, scope
  jobs) and an ordered gate (the wait for a chain's worker). One
  unit key, for place and time. One part builder, at read time. No second
  path kept "for compatibility": `results`, `priority` and the old
  `.handover` place go.
- **Fix the cause.** Ids on failures come from where the job is known, not
  patched on later; ordering per place is a continuation, not a wait with
  a timeout.
- **No new layers.** No base classes, registries, plug-in points or
  options nobody asked for. New names are plain (no leading underscore).
- **Cut while building:** anything in this plan that turns out not to be
  needed by the tests above is left out, and the plan says so.

### The files after 0.6

Today `pipeline.py` (790 lines) holds three things: reading a pipeline,
the job board with its record and folders, and the bookkeeping of units.
0.6 adds time to the third and would push it past 1,000 lines. It is split
by what each part is, and two short files are folded in:

| File | Holds | About |
|---|---|---|
| `engine.py` | `Engine`: register, submit, status, deregister, shutdown; running image jobs and scope jobs | as today |
| `pipeline.py` | a pipeline: reading the YAML into phases, every check made at register, building chains from the resolved step settings, and the state of a registered pipeline, which holds its `Units` and its `Board` | ~300 |
| `units.py` *(new)* | place and time: unit keys, open and close, the cascade, collecting a unit's parts and failures, windows and `previous`, and the job futures and the `cancelling` flag | ~450 |
| `board.py` *(new; takes in `record.py`)* | what a run shows and leaves: the job board, the run record, the hand-over folders and their clean-up | ~300 |
| `job_queue.py` *(was `priority_pool.py`)* | the order: an ordered thread pool and an ordered gate | ~120 |
| `handover.py` | the folder format, and `parts`: the lazy sequence a scoped step reads | ~200 |
| `workers.py`, `worker_script.py` | worker processes; a chain run in one call | as today, plus chains |
| `environments.py`, `conda.py` | unchanged | |

From the outside: **`Engine` takes a `pipeline`; a pipeline has `units`
(what is complete) and a `board` (what is happening); jobs wait in the
`job_queue` and run in `workers`; data between them goes through
`handover`.** The module docstring of `engine.py` says this in five lines.

`Units` and `Board` each have their own lock and never call each other;
the pipeline state composes them, and the engine talks to both. Nothing
today needs the two updated together under one lock. `board.py` owns the
hand-over folders' life (make, clean, leftover) because `handover.py`
must stay standard-library only and loaded by file in every step
environment.

**Naming.** "Chain" is the word for steps that run together in one
worker. The engine's existing "scope completion chain"
(`_handle_scope_complete_chain`) is renamed in the move commit to say
what it does (`close_units`), so the word has one meaning.

The split is a move first (a commit that moves and renames code, with the
0.5 tests green), then the 0.6 changes on top, so the review of each is
plain.

### Cut from the plan by these rules

- Passing a deadline back to a held earlier timepoint: dropped. It is
  only needed when the acquisition gives some timepoints a deadline and
  others none, and the guide says to give every timepoint one. Kept: a
  scope job takes the nearest deadline of the images that completed it
  (one `min`).

## Engine changes

All in `zmart_analysis/`; no new dependency. After the move commit (see
"How it is built"), in the files of that table:

1. **pipeline.py — reading.** `split_phases` takes `timepoints` off a step
   like `scope` and `max_workers`; it belongs to the phase. Refused at
   register, each with a message that says why: `timepoints` without
   `scope`; `timepoints` not a whole number ≥ 1 (`all`: "not in this
   version"); `scope: position` with `timepoints: 1`; `scope: position`
   when `levels` lacks `position`; a step named after an engine key.
2. **pipeline.py — chains.** Built in `register` from the resolved step
   settings (environment and `max_workers` after the pipeline's
   override), per occurrence.
3. **units.py — the unit key.** `scope_key` adds the scope's `timepoint`
   when it has one, also for the step over everything (`{"timepoint": t}`).
   `note_open` opens that per-timepoint unit. `close_signals`,
   `collect_for_scope`, `held_for` and the scoped waits use the key.
   `complete="timepoint"` closes the open units of that timepoint,
   narrowest first.
4. **units.py — what is stored.** `(order, match_scope, (folder, id,
   part_scope))` per result; failures with `submission_idx` and `id`.
5. **units.py — windows and position.** Per place: the last N−1
   timepoints' references are kept; window phases run in timepoint order
   by continuation, never by a blocking wait; each run's output folder is
   kept as the next one's `previous`. A phase with `scope: position`
   starts for an image once its phase 0 is done.
6. **board.py.** The board, the run record (from `record.py`), the
   step-only mark for "at step X", per-step records into the record, and
   the hand-over folders under `Engine(handover=...)` or the run folder,
   with the safe clean-up.
7. **handover.py, worker_script.py.** `parts` replaces `StoredResults`;
   the `RESULTS` marker holds the triples. The worker runs a chain in one
   call: `finish` without the write between its steps, a step message and
   a step record around each, one write at the end; `previous` read from
   its folder.
8. **workers.py.** `answer_arrives` reads the step messages, restarts the
   step timeout at each, and remembers the step for `.step` on errors.
   `WorkerPool.execute` waits at the ordered gate, keyed by the chain.
9. **job_queue.py** (was `priority_pool.py`). The order `(deadline or
   +inf, id)`: an ordered thread pool for image jobs and one for scope
   jobs (they stay two pools, so a scope job never takes the last thread
   its images need), and an ordered gate for the worker wait.
10. **engine.py.** `Engine(..., handover=None)`; `submit(pipeline, data,
    scope=None, deadline=None, complete=None)`; `deadline` and `late` on
    the board and in the record; scope jobs inherit the nearest deadline;
    `_handle_scope_complete_chain` renamed `close_units`.

## Tests first

Engine features are tested with small steps in `tests/`, as today; no
time-lapse workflow is shipped.

New `tests/test_time.py`:

- a `timepoints: 1` step runs once per place per timepoint and keeps
  timepoints apart;
- a step over everything with `timepoints: 1` runs once per timepoint at
  `complete="timepoint"`;
- `complete="timepoint"` closes every open unit of that timepoint and no
  other;
- a `timepoints: 2` phase gets the parts of this timepoint and the one
  before, oldest first; at the first timepoint only this one;
- a skipped timepoint is missing from the window, not empty;
- position 7 of well 3 and of well 4 are separate tracks;
- the window phases of one place run in timepoint order, even when
  timepoint 4's image finishes before timepoint 3's, **and with
  `max_concurrent=1`** (no deadlock);
- `previous` is the phase's last output for the timepoint before; `None`
  at the first, and after a failed one;
- `scope: position` runs without any `complete`;
- a late image reopens its own timepoint; the window at the next
  timepoint is not run again;
- references no window needs any more are released (count what is held);
- register refuses: `timepoints` without `scope`, `timepoints: all`,
  `timepoints: 0`, `scope: position` with `timepoints: 1`, `scope:
  position` without `position` in `levels`, a step named `input`.

New `tests/test_parts.py`:

- an image part has `id` (= the board's job id), `scope`, `output`, and
  `input` when the steps kept it; `output` has no engine keys;
- a per-image step that returned a new dictionary still gives a part
  with the right `id` and `scope`;
- a unit part's `id` is the scope job's id on the board and its `scope`
  the unit;
- a two-step scoped phase: the second step gets the first's output; the
  unit part's `output` is the last step's;
- failures have `id` (the failed job, also for a scope job), `scope`,
  `step`, `error`; `lineage["failed"]` on the board has the same shape;
- parts are read one at a time (the memory test `results` has today);
- a step returning anything but a dictionary fails with the message that
  names a dictionary.

In `tests/test_engine.py`, the priority tests become deadline tests:

- a nearer deadline starts first; no deadline comes last; ties by id;
- the order holds for scope jobs and while jobs wait for a
  `max_workers: 1` step;
- a job that ends after its deadline is `late` on the board and in the
  record;
- `submit(..., priority=...)` raises `TypeError` naming `deadline`.

The scoped steps in the existing tests move from `results` to `parts`:
`tests/test_scopes.py`, `test_job_board.py`, `test_engine.py`,
`test_handover_and_record.py`, and `tests/helpers.py` (`phase_of`). The
0.5 suite otherwise stays green.

## Docs

- Guide 1: a section "Steps that look over time" after the scope section
  (the grid picture, `timepoints`, `complete="timepoint"`, the late-image
  limit); `deadline` replaces `priority` in the submit table; the table of
  a scoped job's output (`lineage["failed"]` shape).
- Guide 2: a section "In and output": the per-image step (unchanged), the
  part with a small picture (parts in, a new dictionary out), the rule
  "the engine fixes the envelope, you own the contents", the reserved
  step names; what a phase over time receives (parts by timepoint,
  `previous`); testing a scoped step on its own with a hand-made list of
  parts.
- Both tutorials: the demo scoped steps read `parts`; re-run with outputs.
- Guide 1: `Engine(handover=...)` in the engine's table, and why to point
  it at a local data disk for large stacks. Guide 2: steps in one
  environment with the same `max_workers` run as one chain, in memory.
- CHANGELOG 0.6.0: the two breaking changes first (`priority` →
  `deadline`, `results` → `parts`), then time.
- README unchanged unless Thom wants a line.

## Out of scope

- `timepoints: all` (no caller; analysis over a whole time-lapse can run
  afterwards on the files). Refused at register.
- The board forgetting old outputs (today every output stays until
  deregister; a long time-lapse keeps its folders). Its own plan if a
  run shows it matters.
- Re-running later windows after a late image.
- A time-lapse workflow in `workflows/`; the interface's acquisition
  sending `timepoint`, `complete="timepoint"` and `deadline`.
- Cancelling or skipping late jobs; deadlines that move after submit;
  windows in seconds.
- The interface's fake engines in `test_warm.py` still declare
  `priority=None`: harmless, tidied when the interface next moves.

## Decisions (Thom, 2026-10-21)

- **D1** First caller: tracking cells per position (`timepoints: 2`, with
  a deadline).
- **D2** A phase over N timepoints gets `previous`, its own output at the
  timepoint before.
- **D3** `deadline` replaces `priority`.
- **D4** No `timepoints: all` in 0.6.
- **D5** Scoped steps get `parts` with a fixed outside (`id`, `scope`,
  `output`, and `input` for an image) and return a new dictionary whose
  inside is free; per-image steps are unchanged; the board keeps its
  shape.
- **D6** The hand-over is part of 0.6: same-environment chains pass data
  in memory; the hand-over can be moved to a local folder
  (`Engine(handover=...)`, the run folder by default); not decided by
  measuring, measured after to report the gain.

## Review A (2026-10-21), and what changed

1. id and scope are not reliably in stored results → the engine stores and
   hands over `(folder, id, scope)`; scope job id and unit for units.
2. Failure ids did not match the board → job ids on failures;
   `lineage["failed"]` takes the shape (the one board change, stated).
3. Wrapping on store would change files and board → parts are built at
   read time; nothing is wrapped.
4. "Output by step name" is not true (`object_analysis`) → `output` is
   what the steps left minus engine keys; by-name is a convention.
5. Multi-step scoped phases were undefined → first step gets parts, the
   next the step before's output; unit part = last step; `timepoints` and
   `previous` per phase.
6. `scope: position` merged positions across wells → `position` must be
   in `levels`; the key carries every wider level.
7. Steps over everything ignored time → one unit per timepoint.
8. Deadlines did not reach scope jobs; inversion; possible deadlock →
   ordered scope-job queue; continuation, no blocking wait; a
   `max_concurrent=1` test. (Passing deadlines back was later cut as
   over-built; see "How it is built".)
9. Retention "on disk" was wrong → only references are released; folders
   stay until deregister (out of scope); late-image limit stated.
10. Engine key names as step names → refused at register.
11. Smaller claims corrected (returning `unit` was never needed;
    "return a new dictionary" is already guide 2; provenance moves to
    lineage only).
12. Missing files added (`helpers.py`, guide tables, CHANGELOG, the
    interface's fake engines).

## Review B (2026-10-21), and what changed

1. Clean-up in a shared temp folder would remove everything it cannot
   read → a folder of the engine's own; unreadable names are left alone.
2. Storing a unit under its scope job's id breaks "up to the closing
   submit" → order and identity kept apart.
3. Defaulting the hand-over to the profile's temp folder would fill the
   28 GB profile, and the focus output holds whole stacks → default stays
   the run folder; `handover=` opt-in, passed by the interface;
   `score_focus` drops the stack.
4. One summary per chain loses per-step times and provenance → the worker
   sends a record per step.
5. "At step X" would flip a running job to queued, and a crash named the
   wrong step → a step-only mark; errors carry `.step`.
6. Summing timeouts is wrong for an engine-wide timeout and `None` → the
   timeout restarts at each step.
7. The worker wait had no key for a chain; overlap of `max_workers: 1`
   steps is lost → keyed by the chain; the trade stated; a batch measured.
8. Chains need resolved settings → built in register, per occurrence.
9. "Identical outputs" cannot hold in general (NumPy scalars, shared
   arrays) → claimed for the shipped pipelines; guide 2 says why.
10. Middle steps skipped `finish` → `finish` without the write.
11. The stored `RESULTS` marker → holds the triples, reads back as parts.
12. Other users' processes and pipeline name collisions → user and
    pipeline in the path; "access denied" counts as alive.
13. The lock and the futures had no owner → `Units` and `Board` own
    locks; futures and `cancelling` in `units.py`.
14. "Chain" meant two things; "one queue" was two pieces → the old chain
    renamed; the order described as a pool and a gate.

## Where the work lands

ZMB-UZH/ZMART-analysis `release-candidate`, version 0.6.0rc1, TDD, after
0.5.0 is promoted to `main`. The interface's one change for 0.6 is passing
`Engine(handover=...)` (a local data disk); the assembly does not change.
