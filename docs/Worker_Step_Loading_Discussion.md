# Worker Step Loading — Discussion & Design Exploration

**Date:** 2026-03-21
**Branch:** `claude/v3-release-mso2Q`
**Status:** Exploration / open discussion

---

## Context

Smart Analysis v3 introduced a fundamentally different worker model compared to
v2. Where v2 had per-step workers (one worker per step file), v3 moved to
**per-environment workers**: a single worker subprocess can execute any step that
declares the same conda environment. This was driven by the need to support
multi-pipeline execution with shared resources, priority scheduling, and
configurable isolation.

This document captures a design discussion about how step modules are currently
loaded into workers, what that implies, and what alternatives might improve the
architecture going forward.

---

## The Current Step Loading Model

### How it works today

The loading path is:

```
Pipeline YAML parsed
  → step_config.name resolved to a .py file in functions_dir
    → METADATA read via AST (no code execution) for routing
      → Worker selected based on environment + device
        → (step_path, pipeline_data, params) sent to worker over TCP socket
          → worker_script.py loads module via exec()
            → module.run(pipeline_data, **params) called
              → result sent back as ("ok", result) or ("error", {...})
```

#### 1. Routing (engine side — `_loader.py`)

Before any code runs, the engine reads step METADATA via AST parsing:

```python
# engine/_loader.py:54-70
def _extract_metadata(step_path):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "METADATA":
                    return ast.literal_eval(node.value)
    return None
```

This extracts `environment` and `device` without executing any step code. The
engine uses these values to decide:

- Which worker (by environment) should receive the step
- Whether the step needs a GPU slot
- Whether isolation requires a separate process

#### 2. Worker selection (engine side — `_pipeline.py:132-157`)

```python
needs_isolation = (
    isolation == "maximal"
    or (target_env.lower() != "local"
        and target_env.lower() != current_env.lower())
)

if needs_isolation:
    return self._pool.execute(
        environment=target_env, device=device,
        step_path=str(func_path), pipeline_data=pipeline_data,
        params=step_config.params, ...
    )
else:
    # In-process: load and run directly
    module = load_function(step_config.name, functions_dir)
    return module.run(pipeline_data, **step_config.params)
```

Two paths:
- **In-process** — step runs in the orchestrator's Python process (same env, minimal isolation)
- **Out-of-process** — step is sent to a worker subprocess via the pool

#### 3. Module loading (worker side — `worker_script.py:52-60`)

Inside the worker subprocess, steps are loaded with `exec()`:

```python
def _load_module(step_path):
    name = os.path.splitext(os.path.basename(step_path))[0]
    namespace = {"__name__": name, "__file__": step_path}
    with open(step_path) as f:
        exec(compile(f.read(), step_path, "exec"), namespace)
    module = types.ModuleType(name)
    module.__dict__.update(namespace)
    return module
```

This approach was chosen over `importlib.import_module()` to avoid Windows DLL
path issues with conda environments. It creates an isolated namespace and
compiles the step file as a standalone module.

#### 4. Module caching (worker side — `worker_script.py:134-138`)

```python
if step_path not in module_cache:
    logger.info("Loading module: %s", step_name)
    module_cache[step_path] = _load_module(step_path)
module = module_cache[step_path]
```

Loaded modules are cached by file path. On the second call to the same step in
the same worker, the module is reused — no file I/O, no compilation, no
re-initialization. This is the "warm-start" benefit of persistent workers.

#### 5. Communication protocol

```
Parent                              Worker subprocess
  │                                      │
  │  spawn (subprocess.Popen)            │
  │ ──────────────────────────────────►  │
  │                                      │  connect back via Client()
  │  ◄────────────────────────────────  │
  │                                      │
  │  send (step_path, data, params)      │
  │ ──────────────────────────────────►  │
  │                                      │  exec + run()
  │  recv ("ok", result)                 │
  │  ◄────────────────────────────────  │
  │                                      │
  │  send None (sentinel = shutdown)     │
  │ ──────────────────────────────────►  │
```

All messages are serialized with `pickle` protocol 2 over TCP localhost sockets
authenticated with `multiprocessing.connection` authkeys.

---

## What This Implies

### The good

1. **Steps are simple.** A step is just a `.py` file with a `METADATA` dict and
   a `run()` function. No base classes, no decorators, no registration. Copy a
   file, change `METADATA`, write `run()`, done.

2. **Environment sharing is automatic.** If three steps declare the same
   environment, they automatically share a worker process. Their modules coexist
   in the same `module_cache`, and data flows between them as plain dicts — no
   serialization overhead.

3. **Warm-start works.** Because workers are persistent (under minimal
   isolation), the second invocation of `segment.run()` reuses the already-loaded
   module. Libraries like PyTorch that take seconds to import are only loaded
   once per worker lifetime.

4. **AST-based routing is safe.** No step code executes during the planning/
   routing phase. The engine can inspect all steps, build the execution graph,
   and allocate workers without importing anything.

### The less good

1. **Environment coupling is implicit.** The relationship between steps that
   share a worker is an emergent property of matching `environment` strings in
   METADATA. There's no explicit declaration of "these steps form a group."

2. **No formal dependency declaration.** A step that does `import torch` will
   fail if its environment doesn't have PyTorch — but this is discovered at
   runtime, not at validation time. The METADATA says which environment, but
   not what packages the step expects.

3. **No shared initialization.** If two steps in the same environment both need
   to load the same large model (e.g., a neural network checkpoint), each step
   loads it independently. There's no mechanism for declaring shared resources
   across steps within an environment.

4. **No pre-validation.** You can't statically verify that a pipeline's steps
   are compatible with their declared environments. A typo in the environment
   name silently routes to a non-existent conda env, which fails only at worker
   spawn time.

---

## Ideas

### Idea 1: The "Library" Concept

Formalize the grouping of steps that share an environment into a **Library** — a
first-class object that owns an environment, a set of steps, and optionally
shared state.

#### What it would look like

```
workflows/rare_event_selection/
  libraries/
    main_lib.py          # declares env, shared init, step registry
  steps/
    preprocess.py        # belongs to main_lib
    segment.py           # belongs to main_lib
    feedback.py          # belongs to main_lib
```

```python
# main_lib.py
LIBRARY = {
    "environment": "SMART--rare_event_selection--main",
    "device": "cpu",
    "steps": ["preprocess", "segment", "feedback"],
    "shared_init": "initialize",  # optional
}

_model = None

def initialize():
    """Called once when the worker starts. Shared across all steps."""
    global _model
    _model = load_expensive_model()

def get_model():
    return _model
```

```python
# segment.py — no METADATA needed, library provides environment
from . import main_lib

def run(pipeline_data, **params):
    model = main_lib.get_model()
    result = model.predict(pipeline_data["preprocess"]["image"])
    pipeline_data["segment"] = {"mask": result}
    return pipeline_data
```

#### Pros

- **Explicit grouping.** The relationship between steps is declared, not
  inferred. You can see at a glance which steps share a worker.
- **Shared initialization.** Large models, database connections, or expensive
  resources are loaded once and shared across steps. Today each step would
  redundantly load these.
- **Single source of truth for environment.** Remove `environment` from
  individual step METADATA. One declaration per group, fewer typos, easier
  refactoring.
- **Validation.** The engine can verify at pipeline parse time that every step
  belongs to a known library and that the library's environment exists.
- **Discoverability.** A library file documents what a group of steps can do,
  what they share, and what environment they need. New contributors can read one
  file instead of grepping through METADATA dicts.

#### Cons

- **Added complexity.** The current model is beautifully simple: one file = one
  step. Adding libraries introduces a new concept, a new file type, and new
  rules for how steps reference libraries.
- **Import mechanics.** If steps import from their library, the `exec()`-based
  loading model needs rework. Either switch to `importlib` (with the Windows DLL
  issues it brings), or implement a custom import mechanism for library modules.
- **Migration cost.** Every existing workflow needs to be updated. Steps that
  currently work standalone would need to be associated with a library.
- **Over-engineering risk.** Most workflows have 3-5 steps. A Library abstraction
  is useful when you have 10+ steps sharing resources, but may be unnecessary
  overhead for simple pipelines.
- **Coupling.** Steps become dependent on their library. A step can no longer be
  copied to a different workflow without also copying (or referencing) the
  library. Today, steps are fully self-contained.

---

### Idea 2: Implicit Library via Convention

Keep the current model but add optional conventions that the engine recognizes:

```python
# steps/__env__.py — special file, auto-detected by engine
ENVIRONMENT = "SMART--rare_event_selection--main"
DEVICE = "cpu"

def shared_init():
    """Called once when worker starts."""
    return {"model": load_model()}
```

Steps in the same directory that don't declare their own `environment` in
METADATA inherit from `__env__.py`. The engine passes `shared_init()` results
into `run()` via a `context` parameter.

#### Pros

- **No breaking changes.** Steps that have their own METADATA keep working.
  Steps that don't get the directory default. Fully backwards compatible.
- **Low ceremony.** One optional file per directory. No new abstractions, no
  library references, no import changes.
- **Shared init without coupling.** The `shared_init()` return value is passed
  by the engine, not imported by the step. Steps remain self-contained `.py`
  files.

#### Cons

- **Directory-based grouping is rigid.** What if two steps in the same directory
  need different environments? You'd need METADATA overrides, which recreates
  the current system.
- **Magic naming.** `__env__.py` is a convention that needs to be documented and
  understood. Not immediately obvious to newcomers.
- **Limited shared state.** Passing shared state through function arguments is
  less flexible than direct imports. The step can't call library methods or
  access library-level caches.

---

### Idea 3: Worker-Level Init Hook

Add a `worker_init` field to the pipeline YAML that specifies a module to run
once when a worker starts:

```yaml
metadata:
  isolation: minimal
  worker_init:
    "SMART--rare_event_selection--main": "steps/init_main.py"

rare-event-selection:
  - preprocess:
  - segment:
      model: fast
```

```python
# steps/init_main.py
import torch

MODEL = None

def init():
    global MODEL
    MODEL = torch.load("checkpoint.pt")
    MODEL.eval()
```

The worker runs `init()` on first connection, and the module stays in the
worker's namespace. Steps access it via `sys.modules` or a registry.

#### Pros

- **No step changes.** Steps don't need to know about the init hook. The engine
  handles it transparently.
- **Pipeline-level control.** Different pipelines using the same steps can have
  different init hooks (or none). The init is a workflow decision, not a step
  property.
- **Clean separation.** Init logic is separate from step logic. Steps stay
  focused on their `run()` function.

#### Cons

- **Cross-module access is awkward.** How does `segment.py` access the model
  loaded by `init_main.py`? Via `sys.modules`, global registry, or injection?
  All options have ergonomic downsides.
- **Ordering guarantees.** The init must complete before any step runs. With
  persistent workers this is straightforward, but with oneshot workers (maximal
  isolation) the init runs every time — defeating the purpose.
- **Environment mismatch risk.** The YAML maps environment names to init
  scripts, but there's no compile-time guarantee that the init script is
  compatible with the environment.

---

### Idea 4: Keep It Simple — Document the Pattern

Don't change the engine. Instead, document the pattern for sharing state across
steps via Python module-level globals and the existing `module_cache`:

```python
# segment.py
import torch

METADATA = {
    "environment": "SMART--rare_event_selection--main",
    "device": "gpu",
}

# Module-level: runs once on first load, cached thereafter
_model = None

def _get_model():
    global _model
    if _model is None:
        _model = torch.load("checkpoint.pt")
        _model.eval()
    return _model

def run(pipeline_data, **params):
    model = _get_model()
    # ...
```

Since the worker caches the module, `_get_model()` is only called once per
worker lifetime. Other steps in the same environment that also need the model
would each have their own lazy loader — which is duplication, but it's simple
and self-contained.

#### Pros

- **Zero engine changes.** Everything works today.
- **Self-contained steps.** Each step file is fully understandable in isolation.
  No external dependencies beyond Python packages.
- **Battle-tested.** This is how Python modules work. Lazy singletons are a
  well-understood pattern.

#### Cons

- **Duplicate loading.** If `segment.py` and `postprocess.py` both load the
  same PyTorch model, it's loaded twice in the same worker process (once per
  module). This wastes memory and startup time.
- **No coordination.** Steps can't share mutable state (e.g., a database
  connection pool, a running subprocess, a memory-mapped file) without global
  hacks.
- **Implicit warm-start dependency.** The pattern relies on worker persistence
  for performance. Under maximal isolation, every call is cold. This isn't
  visible from the step code — it silently becomes slow.

---

## Open Questions

### 1. Is shared initialization actually needed?

The current system has been running successfully without it. Are there real
workflows where two steps in the same environment need access to the same
expensive resource? If yes, how expensive is it to load twice? If it's a 200MB
PyTorch model, loading twice in the same process wastes 200MB RAM and ~2 seconds
of startup. Is that worth the architectural complexity of a Library concept?

**Action:** Profile real workflows to quantify the cost of redundant loading.

### 2. How far should validation go?

Today the engine trusts that METADATA environment names correspond to real conda
envs. Validation happens at worker spawn time (if `conda run -n foo` fails, the
step fails). Should the engine validate environments earlier?

Options:
- **At parse time:** `conda env list` and check that all referenced envs exist.
  Fast, but environments could be created/deleted between parse and execution.
- **At submit time:** Verify env exists when a job is submitted. Catches errors
  earlier but adds latency to submission.
- **At execution time (current):** Fail fast at worker spawn. Simple, no
  validation overhead, but errors surface late.

### 3. Should steps know about their worker?

Currently steps are pure functions: `run(pipeline_data, **params) -> dict`. They
have no access to worker state, other steps' modules, or engine metadata. Should
this change?

Adding a `context` parameter would let the engine pass worker-level state:

```python
def run(pipeline_data, context=None, **params):
    model = context.get("shared_model")
```

But this creates a contract between steps and the engine that doesn't exist
today. Steps would need to handle `context=None` for backwards compatibility,
and the engine would need to decide what goes in context.

### 4. What about step versioning?

Steps currently have an optional `version` field in METADATA, but the engine
doesn't use it. Should the engine enforce version compatibility? What does
"version" even mean for a step? Is it the step's API version (parameter
changes), the algorithm version (same API, different behavior), or the
implementation version (bug fixes)?

### 5. exec() vs importlib

The current `exec()`-based loading avoids Windows DLL path issues with conda
environments. But it also means steps can't do relative imports (`from . import
utils`), which limits code reuse between steps. If a Library concept introduces
inter-step imports, the loading mechanism needs to change. Is there a way to use
`importlib` safely across conda environments on Windows?

### 6. What happens to module_cache under maximal isolation?

Under maximal isolation, each step gets a oneshot worker that exits after one
call. The `module_cache` is empty on every invocation. This means:

- Every call is a cold start (full module load + library imports)
- Lazy singletons (Idea 4) re-initialize every time
- Worker init hooks (Idea 3) run every time

Is maximal isolation meant for debugging/safety only, where performance doesn't
matter? Or should it also support warm-start patterns?

---

## Summary Table

| Idea | Complexity | Breaking? | Shared Init | Validation | Step Simplicity |
|------|-----------|-----------|-------------|------------|-----------------|
| 1. Library concept | High | Yes | Full support | Strong | Reduced (imports) |
| 2. `__env__.py` convention | Low | No | Partial (args) | Moderate | Preserved |
| 3. Worker init hook in YAML | Medium | No | Partial (registry) | Weak | Preserved |
| 4. Document the pattern | None | No | No (per-step) | None | Full |

---

## Next Steps

1. **Gather data.** Profile existing workflows to understand if redundant
   loading is a real cost or a theoretical concern.
2. **Prototype.** If shared init is needed, prototype Idea 2 (`__env__.py`)
   as the lowest-risk option. It's backwards compatible and low ceremony.
3. **Discuss.** Share this document with the team for feedback. The right answer
   depends on how workflows are expected to grow.

---

*This document was generated from a design discussion on 2026-03-21 exploring
how the v3 engine loads step modules into workers, and what improvements might
be worth pursuing.*
