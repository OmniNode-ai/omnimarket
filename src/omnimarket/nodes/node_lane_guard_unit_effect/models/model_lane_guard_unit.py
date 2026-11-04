# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and result for the lane guard unit node (OMN-22192)."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumLaneGuardOperation(StrEnum):
    START = "start"
    LIST = "list"
    STOP = "stop"


class ModelLaneGuardUnitRequest(BaseModel):
    """One operation on a lane's guard unit ``onexlane-<lane>-<id>``.

    ``start`` needs ``command``; ``list`` and ``stop`` name the unit by ``lane`` and ``run_id``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EnumLaneGuardOperation
    lane: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    command: tuple[str, ...] = ()
    timeout_s: int = Field(default=600, ge=1)
    mem_max: str | None = None
    cpu_quota: int | None = None
    kind: str = "lane"


class ModelLaneGuardMember(BaseModel):
    """One process that carries the unit's tag."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pid: int
    age_s: int
    rss_kb: int
    cmd: str


class ModelLaneGuardUnitResult(BaseModel):
    """What one operation did. ``confirmed_empty`` is true only after a re-read of the process table
    found no process tagged with the unit; it is None for ``start`` and ``list``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EnumLaneGuardOperation
    unit: str
    started_pid: int | None = None
    members: tuple[ModelLaneGuardMember, ...] = ()
    killed: tuple[ModelLaneGuardMember, ...] = ()
    systemd_stopped: bool | None = None
    confirmed_empty: bool | None = None
    refusal: str | None = None
