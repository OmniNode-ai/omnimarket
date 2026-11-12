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

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumIntegrationState(StrEnum):
    """Where a merged change stands against the place it runs."""

    RUNNING = "running"
    NOT_RUNNING = "not_running"
    BLOCKED = "blocked"
    DEFECT = "defect"
    # Merged changes that run nowhere by design: counted, never listed as unintegrated.
    CI_ONLY = "ci_only"
    LIBRARY = "library"


class EnumIntegrationGapClass(StrEnum):
    """What it would take to get a change running. The declaration order is the order the floor draws the groups in."""

    NONE = "none"
    NEEDS_FIX = "needs_fix"
    WAITS_ON_MERGE = "waits_on_merge"
    NEEDS_INSTALL = "needs_install"
    NEEDS_PROOF = "needs_proof"
    NEEDS_READING = "needs_reading"


# States that are not unintegrated work: a running change, and a change that runs nowhere by design.
SETTLED_STATES = frozenset(
    {
        EnumIntegrationState.RUNNING,
        EnumIntegrationState.CI_ONLY,
        EnumIntegrationState.LIBRARY,
    }
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
        settled = self.state in SETTLED_STATES
        if settled and self.gap_class is not EnumIntegrationGapClass.NONE:
            raise ValueError(f"a {self.state.value} change has no gap class")
        if not settled:
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
