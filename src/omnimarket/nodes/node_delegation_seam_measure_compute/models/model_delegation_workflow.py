# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed rows of the delegation workflow registry.

Every cell of a row carries the observation it rests on as
``verified_by: <command> => <observation>``; a cell without one does not load.
"""

from __future__ import annotations

import re
from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

_WORKFLOW_ID = re.compile(r"^W([1-9]|1[0-9])$")
_SEAM_ID = re.compile(r"^DS-[1-9]$")
_STATION_ID = re.compile(r"^DSt[1-8]$")


def _require_verified_by(value: str) -> str:
    command, arrow, observation = value.partition(" => ")
    if not (arrow and command.strip() and observation.strip()):
        raise ValueError("verified_by must read '<command> => <observation>'")
    return value


class ModelRegistryCell(BaseModel):
    """One fact of a workflow, with the command that showed it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: str = Field(min_length=1)
    verified_by: str = Field(min_length=1)

    @field_validator("value")
    @classmethod
    def _value_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a cell value must not be blank")
        return value

    @field_validator("verified_by")
    @classmethod
    def _verified_by_shape(cls, value: str) -> str:
        return _require_verified_by(value)


class ModelWorkflowCanary(BaseModel):
    """The canary family of a workflow, or the producer or blocker that stands for it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    family: str = Field(min_length=1)
    ref: str = Field(min_length=1)
    verified_by: str = Field(min_length=1)

    @field_validator("verified_by")
    @classmethod
    def _verified_by_shape(cls, value: str) -> str:
        return _require_verified_by(value)


class ModelStationShape(BaseModel):
    """The canonical shape that crosses into one delegation station."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    station: str = Field(pattern=_STATION_ID.pattern)
    seam: str = Field(pattern=_SEAM_ID.pattern)
    name: str = Field(min_length=1)
    shape: ModelRegistryCell


class ModelDelegationWorkflow(BaseModel):
    """One delegation workflow: its cells, seams, measurement and canary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=_WORKFLOW_ID.pattern)
    name: str = Field(min_length=1)
    entry: ModelRegistryCell
    routing_fallback: ModelRegistryCell
    quality_gate: ModelRegistryCell
    receipt: ModelRegistryCell
    usage_record: ModelRegistryCell
    dashboard_read: ModelRegistryCell
    stations: tuple[str, ...]
    seams: tuple[str, ...]
    shapes: ModelRegistryCell
    measurement: str = Field(pattern=_SEAM_ID.pattern)
    criteria: tuple[str, ...]
    canary: ModelWorkflowCanary
    owner: str = Field(min_length=1)

    @field_validator("stations")
    @classmethod
    def _stations_are_stations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _STATION_ID.match(item) for item in value):
            raise ValueError("stations are DSt1 to DSt8")
        return value

    @field_validator("seams")
    @classmethod
    def _seams_are_seams(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not _SEAM_ID.match(item) for item in value):
            raise ValueError("seams are DS-1 to DS-9 and not empty")
        return value

    def cells(self) -> tuple[ModelRegistryCell, ...]:
        return (
            self.entry,
            self.routing_fallback,
            self.quality_gate,
            self.receipt,
            self.usage_record,
            self.dashboard_read,
            self.shapes,
        )


class ModelDelegationWorkflowRegistry(BaseModel):
    """The registry file: station shapes and the 19 workflows."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    station_shapes: tuple[ModelStationShape, ...]
    workflows: tuple[ModelDelegationWorkflow, ...]

    def check_complete(self) -> Self:
        ids = [workflow.id for workflow in self.workflows]
        expected = [f"W{n}" for n in range(1, 20)]
        if ids != expected:
            raise ValueError(f"registry must hold W1 to W19 in order, got {ids}")
        stations = [shape.station for shape in self.station_shapes]
        if stations != [f"DSt{n}" for n in range(1, 9)]:
            raise ValueError(f"station shapes must hold DSt1 to DSt8, got {stations}")
        return self


class ModelLoadDelegationWorkflowsRequest(BaseModel):
    """Facts for ``load_delegation_workflows``: the read time, and optional row ids."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime
    workflow_ids: tuple[str, ...] = ()


class ModelLoadDelegationWorkflowsResult(BaseModel):
    """The typed rows, stamped with the read time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    read_at: AwareDatetime
    station_shapes: tuple[ModelStationShape, ...]
    workflows: tuple[ModelDelegationWorkflow, ...]
