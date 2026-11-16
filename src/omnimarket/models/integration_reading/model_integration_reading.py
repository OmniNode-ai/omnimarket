# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Integration readings: is a merged change running where it runs (OMN-23899).

The hourly integration pass reads, for each merged change, where it should run and whether it does, and hands the
readings to ``node_integration_reading_publish_effect`` as one typed batch. ``node_integration_projection_compute``
keeps the newest reading per repository and number and derives the counts and the groups the factory floor draws.
The gap class is the pass's own typed judgment of what it would take to get the change running; nothing here infers
it from the words of the gap.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumIntegrationState(StrEnum):
    """Where a merged change stands against the place it runs."""

    RUNNING = "running"
    NOT_RUNNING = "not_running"
    # A proving lane cannot supply a missing caller or schedule. Keep the gap visible.
    NO_CALLER = "no_caller"
    BLOCKED = "blocked"
    DEFECT = "defect"
    # In place or unproven: the change is where it would run, but no run of it has been seen yet; a reading is owed.
    # Counted on its own, never as unintegrated.
    UNPROVEN = "unproven"
    # Merged changes that run nowhere by design: counted, never listed as unintegrated.
    CI_ONLY = "ci_only"
    LIBRARY = "library"


class EnumIntegrationGapClass(StrEnum):
    """What it would take to get a change running: the integration pass's own classes (its ledger counts unintegrated
    work by register, unit, plugin, reader, pin, second and ahead; blocked work waits on a merge; an unproven change
    owes a reading). The declaration order is the order the floor draws the groups in."""

    NONE = "none"
    RUNTIME_BEHIND = "runtime_behind"
    NEEDS_REGISTRATION = "needs_registration"
    NEEDS_UNIT = "needs_unit"
    NEEDS_READER = "needs_reader"
    NEEDS_PIN = "needs_pin"
    NEEDS_PLUGIN_RELEASE = "needs_plugin_release"
    SECOND_COPY = "second_copy"
    WAITS_ON_MERGE = "waits_on_merge"
    NEEDS_READING = "needs_reading"


# States that are not unintegrated work: a running change, and a change that runs nowhere by design.
SETTLED_STATES = frozenset(
    {
        EnumIntegrationState.RUNNING,
        EnumIntegrationState.CI_ONLY,
        EnumIntegrationState.LIBRARY,
    }
)


# Unintegrated work: merged, meant to run, and not running.
UNINTEGRATED_STATES = frozenset(
    {
        EnumIntegrationState.NOT_RUNNING,
        EnumIntegrationState.NO_CALLER,
        EnumIntegrationState.BLOCKED,
        EnumIntegrationState.DEFECT,
    }
)


# Cells that name nothing: a running reading carrying only these has not named its run (OMN-24535).
_RUNNING_CELL_FILLERS = frozenset({"none", "n/a", "na", "tbd", "-", "x", "unknown"})

_RUNNING_CELLS_REQUIRED = (
    "a running change names the entry point that runs it (running_path) and one line from its latest run "
    "(running_line); an image revision, a merge or one ready answer is not a reading (OMN-24535)"
)


class ModelIntegrationReading(_Frozen):
    """One merged change, read against where it runs."""

    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    number: int = Field(gt=0)
    title: str = Field(min_length=1)
    merged_at: datetime
    # Where the change runs (the dev runtime, a unit on a host, CI).
    target: str = Field(min_length=1)
    state: EnumIntegrationState
    gap_class: EnumIntegrationGapClass
    gap: str = ""
    step: str = ""
    recommendation: Literal["WIRE", "RETIRE", "MOVE"] | None = None
    # Caller and profile for WIRE, superseding flow for RETIRE, or host/Temporal schedule for MOVE.
    recommendation_target: str = ""
    # The entry point that executes the change: unit, node, route or tick step.
    running_path: str = ""
    # One line from that entry point's latest run showing the changed behaviour.
    running_line: str = ""
    owner: str = ""
    read_at: datetime
    # Who read it: the pass and its session or job.
    origin: str = Field(min_length=1)
    # Changes that land with this one (``repository#number``); they are not keys of their own.
    companions: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.repository}#{self.number}"

    @model_validator(mode="after")
    def _shape(self) -> ModelIntegrationReading:
        for name, value in (("merged_at", self.merged_at), ("read_at", self.read_at)):
            if value.tzinfo is None:
                raise ValueError(
                    f"{name} carries its time zone; a bare time cannot be aged"
                )
        if self.state is EnumIntegrationState.NO_CALLER and (
            self.recommendation is None or not self.recommendation_target.strip()
        ):
            raise ValueError(
                "a no_caller change names WIRE, RETIRE or MOVE and its recommendation_target"
            )
        if self.state in SETTLED_STATES:
            if self.gap_class is not EnumIntegrationGapClass.NONE:
                raise ValueError(f"a {self.state.value} change has no gap class")
            if self.state is EnumIntegrationState.RUNNING:
                running_path = self.running_path.strip()
                running_line = self.running_line.strip()
                if (
                    not running_path
                    or len(running_path.split()) != 1
                    or running_path.lower() in _RUNNING_CELL_FILLERS
                ):
                    raise ValueError(_RUNNING_CELLS_REQUIRED)
                if (
                    not running_line
                    or running_line.lower() in _RUNNING_CELL_FILLERS
                    or running_line.lower() == running_path.lower()
                ):
                    raise ValueError(_RUNNING_CELLS_REQUIRED)
        elif self.state is EnumIntegrationState.UNPROVEN:
            if self.gap_class is not EnumIntegrationGapClass.NEEDS_READING:
                raise ValueError(
                    "an unproven change owes a reading (gap_class needs_reading)"
                )
            if not self.step:
                raise ValueError("an unproven change names the reading it owes (step)")
        else:
            if self.gap_class is EnumIntegrationGapClass.NONE:
                raise ValueError(
                    f"a {self.state.value} change names what it would take (gap_class)"
                )
            if not self.gap or not self.step:
                raise ValueError(
                    f"a {self.state.value} change names its gap and its one step"
                )
        return self


class ModelIntegrationReadingBatch(_Frozen):
    """One pass's readings, as the pass hands them to the publish effect."""

    as_of: datetime
    origin: str = Field(min_length=1)
    readings: tuple[ModelIntegrationReading, ...]

    @model_validator(mode="after")
    def _as_of_names_its_zone(self) -> ModelIntegrationReadingBatch:
        if self.as_of.tzinfo is None:
            raise ValueError("as_of carries its time zone; a bare time cannot be aged")
        return self


class ModelIntegrationOldest(_Frozen):
    """The oldest unintegrated change."""

    key: str
    merged_at: datetime


class ModelIntegrationCounts(_Frozen):
    """The numbers the floor draws on top of the list."""

    merged: int = Field(ge=0)
    seen_running: int = Field(ge=0)
    settled_elsewhere: int = Field(ge=0)
    reading_owed: int = Field(ge=0)
    unintegrated: int = Field(ge=0)
    by_gap_class: dict[EnumIntegrationGapClass, int]
    oldest_unintegrated: ModelIntegrationOldest | None = None


class ModelIntegrationGroup(_Frozen):
    """The unintegrated changes that would take the same kind of step, oldest merge first."""

    gap_class: EnumIntegrationGapClass
    keys: tuple[str, ...]


class ModelIntegrationProjection(_Frozen):
    """The newest reading per repository and number, the counts and the groups."""

    as_of: datetime
    origin: str
    rows: dict[str, ModelIntegrationReading]
    counts: ModelIntegrationCounts
    groups: tuple[ModelIntegrationGroup, ...]


class ModelIntegrationPublishCommand(_Frozen):
    """The one command the publish effect executes: publish this projection under this id, as of its own time."""

    projection_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    projection: ModelIntegrationProjection


__all__ = [
    "SETTLED_STATES",
    "UNINTEGRATED_STATES",
    "EnumIntegrationGapClass",
    "EnumIntegrationState",
    "ModelIntegrationCounts",
    "ModelIntegrationGroup",
    "ModelIntegrationOldest",
    "ModelIntegrationProjection",
    "ModelIntegrationPublishCommand",
    "ModelIntegrationReading",
    "ModelIntegrationReadingBatch",
]
