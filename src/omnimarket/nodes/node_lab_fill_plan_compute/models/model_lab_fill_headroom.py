# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Headroom and dispatch facts for one lab-fill tick (OMN-20867).

Headroom readings are the lab-host-capacity-advertised events the lab-work serve
process publishes on each pool host. Probes are the runner's placement read.
``terminal_failure_cause`` is the field the runtime's failure-terminal guard reads,
so a result carrying one is published on the contract's failure terminal, a loud
failure event.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)

from .model_lab_fill_plan import ModelLabFillHostCapacity
from .model_lab_fill_plan_config import ModelLabFillHeadroomPolicy


class EnumLabFillPlanFailure(StrEnum):
    """Why a tick must publish a failure terminal."""

    HEADROOM_UNKNOWN = "HEADROOM_UNKNOWN"
    FANOUT_UNPROVEN = "FANOUT_UNPROVEN"


class ModelLabFillHostProbe(BaseModel):
    """The runner's placement read of one host, without headroom."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., min_length=1)
    runner_slots: int = Field(..., ge=0)
    local: bool = False
    error: str | None = None
    limited: str | None = None
    refusal: str | None = None
    login_claude: bool = False
    codex_ok: bool = False
    running_lanes: tuple[str, ...] = ()


class ModelLabFillHeadroomUnknown(BaseModel):
    """One host with idle runner slots and no fresh headroom reading."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    failure: Literal[EnumLabFillPlanFailure.HEADROOM_UNKNOWN] = (
        EnumLabFillPlanFailure.HEADROOM_UNKNOWN
    )
    reason: Literal["missing", "stale", "future"]
    runner_slots: int
    reading_age_seconds: float | None
    max_age_seconds: int


class ModelLabFillHeadroomPlanRequest(BaseModel):
    """A tick's placement probes, bus readings and lane policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tick_id: str = Field(..., min_length=1)
    observed_at: datetime
    probes: tuple[ModelLabFillHostProbe, ...]
    readings: tuple[ModelHostCapacityAdvertisement, ...] = ()
    policy: ModelLabFillHeadroomPolicy = Field(
        default_factory=ModelLabFillHeadroomPolicy
    )
    max_lanes: int = Field(default=8, ge=1, le=12)

    @field_validator("observed_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return value


class ModelLabFillHeadroomPlanResult(BaseModel):
    """Lanes available for a tick and hosts whose headroom is unknown."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tick_id: str
    hosts: tuple[ModelLabFillHostCapacity, ...]
    unknown: tuple[ModelLabFillHeadroomUnknown, ...]
    free: int
    budget: int
    terminal_failure_cause: EnumLabFillPlanFailure | None = None


class ModelLabFillDispatchRecord(BaseModel):
    """One lane's dispatch record read back from the bus."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tick_id: str = Field(..., min_length=1)
    lane: str = Field(..., min_length=1)
    host: str
    detached: bool = True


class ModelLabFillFanoutRequest(BaseModel):
    """A tick's claimed lanes and dispatch records read back from the bus."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tick_id: str = Field(..., min_length=1)
    claimed_lanes: tuple[str, ...]
    records: tuple[ModelLabFillDispatchRecord, ...] = ()


class ModelLabFillFanoutResult(BaseModel):
    """Confirmed fan-out and claimed lanes with no dispatch record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tick_id: str
    fanned_out: int
    confirmed: tuple[ModelLabFillDispatchRecord, ...]
    unconfirmed: tuple[str, ...]
    other_tick_records: int
    terminal_failure_cause: EnumLabFillPlanFailure | None = None
