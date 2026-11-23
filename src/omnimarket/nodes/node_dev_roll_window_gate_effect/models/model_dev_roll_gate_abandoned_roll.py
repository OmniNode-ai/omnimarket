# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A released roll the gate gave up on, kept in the gate's state."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.nodes.node_dev_roll_window_gate_effect.models.enum_dev_roll_gate_abandon_reason import (
    EnumDevRollGateAbandonReason,
)


class ModelDevRollGateAbandonedRoll(BaseModel):
    """One roll that never completed: its command id, why it was abandoned and both times."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_correlation_id: str = Field(min_length=1)
    git_ref: str = Field(min_length=1)
    reason: EnumDevRollGateAbandonReason
    runtime_started_at: datetime
    abandoned_at: datetime

    @field_validator("runtime_started_at", "abandoned_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("an abandoned roll time must carry a UTC offset")
        return value
