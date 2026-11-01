# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The consumer-group census a bus reports, and the verdict against its contract threshold (OMN-21818)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelConsumerGroupGuardRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    bus_lane: str = Field(min_length=1)


class ModelConsumerGroupCensus(BaseModel):
    """Group counts by state. A census that could not be read is FAIL, never zero groups."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    bus_lane: str
    broker: str
    observed_at: datetime
    status: Literal["OK", "FAIL"]
    groups_total: int | None = Field(default=None, ge=0)
    groups_empty: int | None = Field(default=None, ge=0)
    groups_other: int | None = Field(default=None, ge=0)
    empty_group_max: int = Field(ge=0)
    detail: str
    error: str = ""
    groups_reaped: int = Field(default=0, ge=0)
    reap_detail: str = ""


class ModelReapRules(BaseModel):
    """Reap limits and name rules, all declared in the node contract (OMN-21818)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    enabled: bool
    min_empty_age_seconds: int = Field(ge=0)
    max_deletes_per_run: int = Field(ge=0)
    batch_size: int = Field(ge=1)
    reapable_patterns: tuple[str, ...]
    protected_patterns: tuple[str, ...]


class ModelConsumerGroupReapRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    bus_lane: str = Field(min_length=1)
    state_dir: str = Field(min_length=1)


class ModelConsumerGroupReapResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    bus_lane: str
    groups_reaped: int = Field(ge=0)
    detail: str
