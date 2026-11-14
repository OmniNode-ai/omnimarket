# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The events node_dev_roll_window_gate_effect publishes (observability of every hold, refusal, decision and roll)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.nodes.node_dev_roll_window_gate_effect.models.enum_dev_roll_gate_refusal import (
    EnumDevRollGateRefusal,
)


def _aware(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("a gate event time must carry a UTC offset")
    return value


class ModelDevRollGateHeld(BaseModel):
    """One dev deploy command was held for its window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_correlation_id: UUID
    repository: str
    sha: str
    held_count: int = Field(ge=1)
    observed_at: datetime

    _aware_observed = field_validator("observed_at")(_aware)


class ModelDevRollGateRefused(BaseModel):
    """One deploy command was not held; nothing was stored for it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_correlation_id: UUID | None
    reason: EnumDevRollGateRefusal
    detail: str
    observed_at: datetime

    _aware_observed = field_validator("observed_at")(_aware)


class ModelDevRollGateDecision(BaseModel):
    """One tick's decision: hold (with the batching node's reason) or roll now."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    action: Literal["hold", "roll_now", "refused"]
    hold_reason: str | None
    hold_until: datetime | None
    held_count: int = Field(ge=0)
    released_count: int = Field(ge=0)
    longest_wait_seconds: int = Field(ge=0)
    observed_at: datetime

    _aware_observed = field_validator("observed_at")(_aware)


class ModelDevRollGateRollRecorded(BaseModel):
    """The agent's completion for a released command was recorded as the last roll's end."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_correlation_id: str
    outcome: Literal["finished_healthy", "finished_unhealthy"]
    observed_at: datetime

    _aware_observed = field_validator("observed_at")(_aware)
