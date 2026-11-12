# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed sweep request/result and the stored record of node_host_down_reading_effect."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_host_down_compute.models.model_host_down import (
    EnumHostState,
    ModelHostReading,
)


class ModelHostDownSweepRequest(BaseModel):
    """One sweep: probe every available host and publish the down readings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = ""


class EnumSweepOutcome(StrEnum):
    """The outcome of one sweep."""

    SWEPT = "swept"
    FAILED = "failed"


class ModelHostDownSweepResult(BaseModel):
    """The result of one sweep: what was read, what is down, and how it ended."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumSweepOutcome
    correlation_id: str = ""
    readings: tuple[ModelHostReading, ...] = ()
    down_hosts: tuple[str, ...] = ()
    failure_code: str = ""
    detail: str = ""


class ModelHostDownRecord(BaseModel):
    """One stored down reading of one host: the state and the anchors a reader needs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_name: str = Field(min_length=1)
    state: EnumHostState
    first_failed_at: datetime | None
    down_since: datetime | None
    at: datetime
