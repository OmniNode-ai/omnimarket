# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the start-path census and refusal drill (rebuild plan sections 7.1, 11 and W28).

The census lists every path that starts an agent, session or provisioning call, each ``live`` with the binding that
calls start admission or ``dead``. A start seen on a path the census does not list fails it. The drill plants a bypassing
start on a path and reads the admission answer: the path refuses it only when its enforcement is ``enforce``.

No path list and no mode lives here: the paths arrive from the ``start_path_census`` overlay and the mode of each path
from the ``start_admission`` policy the admission effect reads.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.model_start_admission import (
    EnumAdmissionMode,
    EnumAdmissionOutcome,
    ModelStartAdmissionPolicy,
)

TAG_DRILL = "drill"
INSTANCE_TEST = "test"


class EnumStartPathStatus(StrEnum):
    """Whether a path is wired to start admission (live) or no longer starts anything (dead)."""

    LIVE = "live"
    DEAD = "dead"


class EnumDrillVerdict(StrEnum):
    """What the drill read on one path."""

    NOT_RUN = "not_run"
    OBSERVING = "observing"
    PASS = "pass"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"


class ModelStartPathEntry(BaseModel):
    """One census row: a live path names its binding, a dead path names why it is dead."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path_id: str = Field(min_length=1)
    status: EnumStartPathStatus
    binding: str = ""
    reason: str = ""

    @model_validator(mode="after")
    def _live_has_binding_dead_has_reason(self) -> ModelStartPathEntry:
        if self.status is EnumStartPathStatus.LIVE and not self.binding:
            raise ValueError(f"live path {self.path_id!r} names no binding")
        if self.status is EnumStartPathStatus.DEAD and not self.reason:
            raise ValueError(f"dead path {self.path_id!r} names no reason")
        return self


class ModelStartPathCensusPolicy(BaseModel):
    """The ``start_path_census`` overlay: the paths, as deployment data."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    paths: tuple[ModelStartPathEntry, ...]


class ModelObservedStart(BaseModel):
    """A start seen on a path: ``failure`` is an operational failure the start carried, if any."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path_id: str
    tag: str = ""
    instance: str = ""
    failure: str = ""


class ModelDrillObservation(BaseModel):
    """The admission answer to one bypassing start the drill planted on a path."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path_id: str
    tag: str = TAG_DRILL
    instance: str = INSTANCE_TEST
    mode: EnumAdmissionMode
    outcome: EnumAdmissionOutcome
    proceed: bool


class ModelStartPathCensusRequest(BaseModel):
    """Everything one census reading needs; the node reads no store and no file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    entries: tuple[ModelStartPathEntry, ...]
    admission_policy: ModelStartAdmissionPolicy
    observed_starts: tuple[ModelObservedStart, ...] = ()
    drills: tuple[ModelDrillObservation, ...] = ()

    @model_validator(mode="after")
    def _entries_are_unique(self) -> ModelStartPathCensusRequest:
        ids = [entry.path_id for entry in self.entries]
        if len(ids) != len(set(ids)):
            raise ValueError("the census lists a path more than once")
        return self


class ModelStartPathReading(BaseModel):
    """One path's reading: live or dead, its enforcement and what the drill read there."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path_id: str
    status: EnumStartPathStatus
    binding: str = ""
    enforcement: EnumAdmissionMode
    drill: EnumDrillVerdict
    drill_reasons: tuple[str, ...] = ()


class ModelStartPathCensusResult(BaseModel):
    """The census reading: every path, every way it failed, and the operational failures it kept."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    passed: bool
    paths: tuple[ModelStartPathReading, ...]
    unlisted_starts: tuple[str, ...] = ()
    dead_path_starts: tuple[str, ...] = ()
    drill_failures: tuple[str, ...] = ()
    operational_failures: tuple[str, ...] = ()
