# Smart Analysis Engine v3 — Design

## Overview

Redesign of the pipeline engine to support interleaved multi-pipeline execution
with scoped triggering, priority scheduling, and intelligent worker management.

The engine derives worker layout, process grouping, serialization boundaries,
and scheduling from three user-defined inputs:

1. **Order** — step sequence in the YAML
2. **Scope** — when a step can run (what data must be complete)
3. **Environment** — what the step needs to run (in step METADATA)

---

## Pipeline YAML (pipeline author)

The YAML defines workflow structure: step order, parameters, scope, and isolation.

```yaml
metadata:
  isolation: minimal       # or maximal
  functions_dir: "../steps"

overview-workflow:
  - preprocess:
      sigma: 1.0

  - segment:
      model: fast

  - feedback:

  - stitch:
      scope:
        spatial: region

  - tracking:
      scope:
        temporal: timepoint

  - normalize:
      scope:
        spatial: carrier
        temporal: session
```

### Scope

Scope controls when a step is allowed to run, based on data completeness.

- **No scope** = immediate. The step runs as soon as the previous step completes.
- **Scope is a dict** with two optional keys: `spatial` and `temporal`.
- Scope values (`region`, `timepoint`, `carrier`, `session`) are **user-defined
  strings**. They are not hardcoded in the engine. The engine matches them against
  completion signals without interpreting their meaning.
- Steps with a scope wait until `scope_complete()` is called with matching keys.
- Scoped steps receive **accumulated results** from all jobs that completed the
  preceding immediate steps in that scope group.

### Data at scope boundaries

When a scope completes, the engine collects the output of the **last immediate
step** from every job in that scope group and passes them as a list (in
submission order) to the next step:

```yaml
overview-workflow:
  - segment:              # immediate, runs per tile
  - cluster:              # scoped, gets ALL segment outputs
      scope:
        spatial: region
```

```python
# cluster.py receives:
pipeline_data = {
    "results": [
        seg_result_tile1,   # pipeline_data from tile 1 after segment
        seg_result_tile2,   # pipeline_data from tile 2 after segment
        seg_result_tile3,   # pipeline_data from tile 3 after segment
    ],
    "metadata": {...},
}

def run(pipeline_data, **params):
    all_masks = [r["segment"]["mask"] for r in pipeline_data["results"]]
    # cluster across all tiles
```

The step author accesses individual job results via `pipeline_data["results"]`.
Submission order is preserved in the list.

### Scope only narrows

Scopes form a one-way funnel. Once data is aggregated, it stays aggregated or
narrows further — it never fans back out.

```
immediate  →  spatial  →  temporal  →  both
(per tile)    (region)    (session)    (everything)
```

This follows naturally from what analysis does: aggregation is lossy. A scoped
step reduces many results into one (e.g., 9 masks → 1 stitched image). You
can't un-aggregate. Each scope boundary produces a single output that flows
forward to the next step.

There is no nested scoping within the same axis (e.g., `region` then `carrier`
on the spatial axis). A second scope boundary must be on a different axis or
a combination.

Examples of scope combinations:

| Step | Scope | Triggers when |
|------|-------|---------------|
| preprocess | (none) | Immediately after submission |
| segment | (none) | After preprocess completes |
| stitch | `spatial: region` | All tiles for a region have been processed |
| tracking | `temporal: timepoint` | All timepoints for a position have been processed |
| normalize | `spatial: carrier, temporal: session` | All spatial and temporal data is complete |

### Isolation

Controls how aggressively the engine separates steps into processes.

- **`minimal`** — Group steps that share an environment into the fewest possible
  processes. Data stays in memory (no serialization) within a process. Pickle
  only happens at environment boundaries. Fast, but a crash in one step affects
  others in the same process.
- **`maximal`** — Every step gets its own process. Full pickle round-trip between
  every step. Safest, but slowest due to serialization overhead.

The isolation setting is pipeline-level. The engine uses it together with step
environments to determine the process layout automatically.

---

## Step METADATA (step author)

Each step declares its execution requirements. The engine reads METADATA via
AST parsing — no module code is executed during routing.

```python
METADATA = {
    "environment": "SMART--segment",   # conda env name, or "local"
    "device": "gpu",                   # "gpu" or "cpu" (default)
}
```

### environment

Which conda environment the step requires. Steps that share an environment can
share a process (under minimal isolation). Steps with different environments
require separate processes, with data serialized across the boundary.

`"local"` means the step runs in the orchestrator's own environment.

### device

Declares the physical resource the step needs.

- **`"gpu"`** — The step requires GPU access. Only one GPU worker can be alive
  at a time (VRAM constraint). Different environments that both declare
  `device: "gpu"` are mutually exclusive — the pool shuts down one GPU worker
  before starting another. This applies regardless of framework (PyTorch,
  TensorFlow, etc.).
- **`"cpu"`** (or absent) — No resource constraint. CPU workers are cheap
  (~50-100MB RAM each) and can be spawned freely.

The step author does not specify `worker`, `max_workers`, or isolation settings.
The engine derives these from environment, device, and the pipeline's isolation
setting.

---

## User-facing API

> **Note:** All code in this section is illustrative — it shows the intended API
> shape and usage patterns, not final implementation.

### Engine and Runs

```python
engine = PipelineEngine()

# Create runs — lightweight objects: YAML reference + priority + scope tracker
overview = engine.create_run("overview_pipeline.yaml", priority="high")
target   = engine.create_run("target_pipeline.yaml")

# Submit jobs with scope labels
overview.submit("t0_tile1", data,
                spatial={"region": "R3"},
                temporal={"timepoint": "t0"})
overview.submit("t0_tile2", data,
                spatial={"region": "R3"},
                temporal={"timepoint": "t0"})
target.submit("t0_pos1", data,
              spatial={"region": "R3"},
              temporal={"timepoint": "t0"})

# Signal scope completion
overview.scope_complete(spatial={"region": "R3"}, temporal={"timepoint": "t0"})
```

- **One engine, multiple runs, shared workers.** The engine owns the worker pool.
  Runs are lightweight and independent.
- **Priority is opt-in.** No priority specified = FIFO (submission order). When
  priority is set, higher-priority work goes first at contested resources.
- **`scope_complete()` is an explicit call.** The engine does not count or track
  acquisitions. The calling layer (e.g., smart-microscopy) knows when data is
  complete and signals the engine.
- **Backwards compatible.** Everything works without scopes or priority — submit
  jobs, they run in order, same as v2.

---

## Worker Management

### Workers are per-environment

A worker is a subprocess running in a specific conda environment. It can execute
**any step** that matches its environment — it is not tied to a single step file.

```
Worker (local):           preprocess → feedback → stitch → tracking
Worker (SMART--segment):  ov_segment → t_segment → ov_segment → ...
```

When a worker receives a step it has already executed, the module is cached in
memory (warm). A different step in the same environment loads fresh but stays in
the same process — no serialization cost, no subprocess spawn.

### Process grouping

Under **minimal isolation**, the engine groups consecutive steps that share an
environment into one worker. Data flows as a dict in memory — no pickle.

```
Worker 1 (local):
  preprocess(data) → feedback(data)       ← dict in memory, no pickle
           │
           │  pickle (environment boundary)
           ▼
Worker 2 (SMART--segment):
  segment(data)                           ← dict in memory
```

Serialization (pickle) only happens where the conda environment changes. This is
the minimum possible overhead.

Under **maximal isolation**, every step gets its own worker process, with full
pickle between each step.

### GPU slot management

GPU is a mutually exclusive resource. Only one GPU worker can be alive at a time,
regardless of which environment or framework it uses.

The pool **batches by priority** to minimize expensive cold-start switches:

```
Good: ov_seg → ov_seg → ov_seg → [switch] → t_seg → t_seg → t_seg
Bad:  ov_seg → [switch] → t_seg → [switch] → ov_seg → [switch] → t_seg
```

Sequence:
1. Drain all pending high-priority GPU work
2. Shut down the current GPU worker (free VRAM)
3. Spin up the next GPU worker (different environment/model)
4. Drain its pending work

CPU workers have no such constraint and can coexist freely.

---

## Scheduling

When a step becomes ready to execute, the engine checks five conditions in order:

1. **Order** — All preceding steps in the pipeline must be complete.
2. **Scope** — `scope_complete()` must have been called for every key in the
   step's scope dict.
3. **Resource** — A worker with the right environment must be available. For GPU
   steps, the GPU slot must be free.
4. **Priority** — If multiple steps are waiting for the same resource, higher
   priority goes first.
5. **FIFO** — Within the same priority level, submission order determines
   execution order.

No priority = no surprises. The default is simple FIFO. Priority is opt-in and
only affects ordering at contested resources.

---

## Architecture Layers

```
Microscopy control        Knows the acquisition plan.
(smart-microscopy)        Calls scope_complete() when data is ready.
       │
       ▼
Runs                      Lightweight objects. Each owns a YAML reference,
                          priority level, and scope state. Accumulates
                          results per scope group.
       │
       ▼
PipelineEngine            Central orchestrator. Schedules steps across
                          all runs. Manages the worker pool.
       │
       ▼
WorkerPool                Resource manager. Owns all workers. Handles
                          GPU slot (mutual exclusion), priority queue,
                          and worker lifecycle (spawn, reuse, shutdown).
       │
       ▼
Workers                   Per-environment subprocesses. Execute any step
                          matching their environment. Cache loaded modules.
                          Communicate via TCP sockets with pickle
                          serialization.
```

---

## Observability

Because all work flows through one engine, the full system state is queryable
for dashboards and monitoring.

> **Note:** The structure below is illustrative — it shows the kind of
> information exposed, not the final API shape.

```python
engine.status()
```

```python
{
    "workers": [
        {"env": "local", "device": "cpu", "state": "busy", "current_step": "preprocess"},
        {"env": "SMART--segment", "device": "gpu", "state": "idle", "vram": "4.2GB"},
    ],
    "runs": [
        {"name": "overview", "priority": "high", "pending": 3, "completed": 12},
        {"name": "target", "priority": "normal", "pending": 1, "completed": 5},
    ],
    "queue": [
        {"run": "overview", "step": "segment", "waiting_for": "gpu"},
        {"run": "target", "step": "segment", "waiting_for": "gpu"},
    ],
    "scopes": [
        {"run": "overview", "scope": {"spatial": "R3"}, "jobs": 9, "complete": 6},
    ],
}
```

This covers: worker health, run progress, queue depth, scope completion status,
and resource contention. A dashboard polls `engine.status()` and renders it.

---

## Open Questions

- **Partial failure at scope boundaries.** If one job in a scope group fails
  (e.g., tile 2 fails at segment but tiles 1 and 3 succeed), should the scoped
  step run with partial results or should the entire scope group fail? This needs
  further consideration based on real-world failure modes.

---

## Design Decisions Summary

| Decision | Rationale |
|----------|-----------|
| Scope in YAML, not in step METADATA | Same step can run at different scopes in different pipelines. Scope is a workflow choice, not a step property. |
| Scope is a dict with `spatial` and `temporal` keys | Two independent axes of data completeness. A step can require one, both, or neither. |
| Scope names are user-defined strings | Avoids hardcoding microscopy concepts. Works for `region`, `carrier`, `compartment`, `session`, or any future grouping. |
| `scope_complete()` is an explicit call | The engine is generic — it does not know about tiles, timepoints, or acquisition plans. The calling layer decides when data is complete. |
| Workers are per-environment, not per-step | Steps sharing an environment share a process. Avoids unnecessary serialization. A worker can execute any step sent to it. |
| `device: "gpu"` = mutually exclusive slot | Only one GPU worker alive at a time. Prevents VRAM exhaustion when different frameworks (PyTorch, TensorFlow) share one GPU. |
| GPU work batched by priority | Minimizes cold-start switches. Drain all high-priority GPU work before switching to a different model/environment. |
| `isolation: minimal/maximal` at pipeline level | One knob. Minimal = fewest processes, fastest. Maximal = full isolation, safest. Engine derives process layout automatically. |
| Priority is opt-in, default is FIFO | No surprises. Simple submission-order execution unless the user explicitly sets priority. |
| One engine, multiple runs | Runs are lightweight (YAML + priority + scope state). The engine owns the shared worker pool. No need to manage pools manually. |
| Scope only narrows | Aggregation is lossy — once data is combined at a scope boundary, it stays combined. No fan-out, no re-accumulation. Scopes form a one-way funnel. |
| No nested scoping on the same axis | No `region` then `carrier` on spatial. A second scope boundary must be on a different axis or a combination of axes. |
| All work through one engine | Enables full observability: worker health, queue depth, run progress, scope status. Dashboard polls `engine.status()`. |
| Backwards compatible | Everything works without scopes, priority, or isolation settings. Plain YAML with a step list runs exactly like v2. |
