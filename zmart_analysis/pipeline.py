"""
Pipeline -- a pipeline read from its YAML, and the state of a registered one.

A pipeline (YAML) lists steps; ``split_phases`` cuts the list into phases at
every ``scope``: phase 0 runs per image, each later phase once per unit of
its level.

    Phase 0 (immediate):   preprocess -> segment     [runs per job]
    -- scope: group --
    Phase 1 (scoped):      stitch -> features         [runs once per group]
    -- scope: all --
    Phase 2 (scoped):      normalize                   [runs once for all]

A scoped phase may also say how many ``timepoints`` it looks at: 1 (the
default) for its unit at one timepoint, or N for that timepoint and the
N-1 before it (a window; see units.py). ``check_phases`` refuses, at
register, what this version does not do.

A registered pipeline is a ``PipelineState``: its phases and step settings,
its ``units`` (units.py: what belongs together, and what is complete) and
its ``board`` (board.py: the jobs, the run record and the hand-over
folders). The two never call each other; what both depend on, the jobs'
futures and the ``cancelling`` flag, is kept here.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import yaml

from .board import Board
from .handover import ENGINE_KEYS
from .units import Units

#: Names a step cannot have: its output would land on a key the engine uses.
ENGINE_NAMES = (*ENGINE_KEYS, "parts", "failures", "previous")

# -- Data structures ---------------------------------------------------


@dataclass
class StepConfig:
    """Configuration for one step in a pipeline phase.

    Where a step runs is not configured here. The step file names its own
    conda environment in ``METADATA``, beside the imports that need it, so a
    pipeline describes only the analysis: the steps, their order, their
    parameters and their scopes. Which environment each step actually used,
    and the package versions in it, is recorded in every result under
    ``provenance``.
    """

    name: str
    params: dict[str, object]
    #: The pipeline's word on how many of this step may run at once, over
    #: the step file's own METADATA. A step file is written for its heaviest
    #: caller; a pipeline whose work is light (a watershed, not Cellpose)
    #: may run the same file wide without a second copy of it.
    max_workers: int | None = None


@dataclass
class Chain:
    """Consecutive steps of a phase that run as one call to one worker.

    They share an environment and a ``max_workers``; the output of one is
    handed to the next in memory, and only the chain's last output is
    written. Each step keeps its own ``max_workers`` (``widths``).
    """

    steps: list[StepConfig]
    #: The index in its phase of the chain's first step.
    first: int
    environment: str | None
    widths: list[int]

    @property
    def last(self):
        """The name of the chain's last step."""
        return self.steps[-1].name


def width_of(step, step_settings):
    """How many of *step* may run at once: the pipeline's word, else the file's."""
    if step.max_workers is not None:
        return step.max_workers
    return step_settings[step.name]["max_workers"]


def chains_of(steps, step_settings):
    """A phase's steps, cut into chains wherever the environment or the
    ``max_workers`` changes. ``max_workers`` is the step's own in the
    pipeline, else its file's."""
    chains = []
    for index, step in enumerate(steps):
        settings = step_settings[step.name]
        width = width_of(step, step_settings)
        last = chains[-1] if chains else None
        if last and last.environment == settings["environment"] and last.widths[-1] == width:
            last.steps.append(step)
            last.widths.append(width)
        else:
            chains.append(Chain([step], index, settings["environment"], [width]))
    return chains


@dataclass
class Phase:
    """A group of sequential steps with an optional scope trigger."""

    steps: list[StepConfig]
    scope: str | None = None
    #: How many timepoints of its unit the phase looks at.
    timepoints: int = 1

    @property
    def window(self):
        """Whether the phase looks back at earlier timepoints."""
        return self.timepoints > 1


# -- YAML parsing ------------------------------------------------------


def parse_yaml(yaml_path):
    """
    Parse a pipeline YAML file.

    Returns
    -------
    tuple of (workflow_name, steps_config, metadata)
    """
    yaml_path = Path(yaml_path)
    with open(yaml_path) as f:
        config = yaml.safe_load(f)

    metadata = config.get("metadata", {})

    workflow_name = None
    for key in config:
        if key != "metadata":
            workflow_name = key
            break

    if not workflow_name:
        raise ValueError("No workflow found in YAML (need a key other than 'metadata')")

    steps = config[workflow_name] or []
    if not steps:
        raise ValueError(f"Workflow '{workflow_name}' has no steps")

    return workflow_name, steps, metadata


def split_phases(steps_config):
    """
    Split a flat step list into phases at scope boundaries.

    A new phase starts when a step declares a scope. Steps before the
    first scope are Phase 0 (immediate). Each subsequent scope starts
    a new phase.

    ``scope``, ``timepoints`` and ``max_workers`` are the engine's keys on
    a step and are taken off before the rest reaches the step as its
    params. ``timepoints`` belongs to the phase its scoped step starts. An
    ``environment`` key is refused: the step file owns its environment.
    """
    phases = []
    current_steps = []
    current_scope = None
    current_timepoints = 1

    for step_dict in steps_config:
        name = list(step_dict.keys())[0]
        raw_params = dict(step_dict[name] or {})
        scope = raw_params.pop("scope", None)
        timepoints = raw_params.pop("timepoints", None)
        if timepoints is not None and scope is None:
            raise ValueError(
                f"Step '{name}' sets 'timepoints' without a 'scope': only a scoped "
                "step looks at a unit over time."
            )
        if "environment" in raw_params:
            raise ValueError(
                f"Step '{name}' sets 'environment' in the pipeline YAML. The "
                "step file owns its environment: set it in the step's "
                "METADATA, or write a separate step file for a different "
                "environment."
            )
        max_workers = raw_params.pop("max_workers", None)
        if name in ENGINE_NAMES:
            raise ValueError(
                f"A step is named {name!r}, a key the engine uses in what it hands "
                f"a step ({', '.join(ENGINE_NAMES)}); give the step another name."
            )

        if scope is not None:
            if current_steps:
                phases.append(Phase(current_steps, current_scope, current_timepoints))
            current_steps = []
            current_scope = scope
            current_timepoints = checked_timepoints(name, 1 if timepoints is None else timepoints)

        current_steps.append(
            StepConfig(
                name=name,
                params=raw_params,
                max_workers=int(max_workers) if max_workers is not None else None,
            )
        )

    if current_steps:
        phases.append(Phase(current_steps, current_scope, current_timepoints))

    return phases


def checked_timepoints(name, timepoints):
    """``timepoints`` as a whole number of 1 or more, or the reason it is not."""
    if timepoints == "all":
        raise ValueError(f"Step '{name}': 'timepoints: all' is not in this version; give a number.")
    if isinstance(timepoints, bool) or not isinstance(timepoints, int) or timepoints < 1:
        raise ValueError(
            f"Step '{name}': 'timepoints' must be a whole number of 1 or more, got {timepoints!r}"
        )
    return timepoints


def check_phases(phases, levels):
    """Refuse, at register, what a pipeline over time cannot do in this version.

    A window (``timepoints`` of 2 or more) is the last scoped phase: a phase
    after it would wait, in a thread, on a window held for its timepoint
    before. ``scope: position`` is the first scoped phase, runs as soon as
    its image is done, and needs ``position`` in ``levels`` so that position
    7 of one well is not position 7 of another; one position at one
    timepoint is one image, so it is only scoped over several timepoints.
    """
    scoped = [phase for phase in phases if phase.scope]
    for i, phase in enumerate(scoped):
        name = phase.steps[0].name
        if phase.window and i != len(scoped) - 1:
            raise ValueError(
                f"Step '{name}' looks at {phase.timepoints} timepoints, so it must "
                "be the last scoped step of the pipeline."
            )
        if phase.scope == "position":
            if i != 0:
                raise ValueError(
                    f"Step '{name}' has scope 'position', so it must be the first "
                    "scoped step of the pipeline."
                )
            if not phase.window:
                raise ValueError(
                    f"Step '{name}' has scope 'position' at one timepoint, which is one "
                    "image: run it per image, or give it 'timepoints' of 2 or more."
                )
            if not levels or "position" not in levels:
                raise ValueError(
                    f"Step '{name}' has scope 'position': add 'position' to the "
                    "pipeline's metadata 'levels', so the same position in two "
                    "wells stays two places."
                )


# -- A registered pipeline -------------------------------------------


class PipelineState:
    """One registered pipeline: its phases, its units and its board."""

    def __init__(
        self,
        name,
        yaml_path,
        phases,
        functions_dir,
        step_settings,
        verbose,
        levels=None,
        workflow_name=None,
        *,
        output,
        registration,
        handover=None,
    ):
        self.name = name
        self.yaml_path = Path(yaml_path)
        self.phases = phases
        self.functions_dir = functions_dir
        self.step_settings = step_settings
        self.verbose = verbose
        #: The pipeline's top-level key, for the results' metadata.
        self.workflow_name = workflow_name or name
        #: Per phase, its steps cut into chains (see chains_of).
        self.chains = [chains_of(phase.steps, step_settings) for phase in phases]
        self.units = Units(phases, levels)
        self.board = Board(name, phases, Path(output) / "analysis" / name, registration, handover)

        self.lock = threading.Lock()
        #: Every future this pipeline started, image jobs and scope closes, so
        #: deregister can wait for them or cancel them.
        self.futures = []
        #: Set by deregister(wait=False): no step of this pipeline starts again.
        self.cancelling = False

    @property
    def position_phase(self):
        """Whether the pipeline's first scoped phase is over positions."""
        return len(self.phases) > 1 and self.phases[1].scope == "position"

    @property
    def environments(self):
        """The environments this pipeline's steps run in."""
        return {settings["environment"] for settings in self.step_settings.values()}

    def add_image_job(self, future, scope, job):
        """Keep an image job's future: for deregister, for the closes of its
        units, and to cancel its board entry if it never ran."""
        with self.lock:
            self.futures.append(future)
        self.units.add_entry(future, scope, job)
        future.add_done_callback(
            lambda done: self.board.job_cancelled(job) if done.cancelled() else None
        )

    def add_future(self, future):
        """Keep a scope close's future, for deregister."""
        with self.lock:
            self.futures.append(future)

    def unfinished(self):
        """The futures of this pipeline that have not finished."""
        with self.lock:
            return [future for future in self.futures if not future.done()]

    def job_failed(self, job, error, step=None, steps=()):
        """The job failed; a job stopped by deregister is cancelled instead."""
        if self.cancelling:
            self.board.job_cancelled(job, steps)
        else:
            self.board.job_failed(job, error, step, steps)
