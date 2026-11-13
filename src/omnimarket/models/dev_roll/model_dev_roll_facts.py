# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One decision's input: the pending tip events and the last roll."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from omnimarket.models.dev_roll.model_dev_roll_record import (
    ModelDevRollRecord,
)
from omnimarket.models.dev_roll.model_dev_tip_changed import (
    ModelDevTipChanged,
)


class ModelDevRollFacts(BaseModel):
    """The compute reads no clock: ``observed_at`` is the snapshot's time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    observed_at: datetime
    pending_events: tuple[ModelDevTipChanged, ...]
    last_roll: ModelDevRollRecord | None

    @field_validator("observed_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("observed_at must carry a UTC offset")
        return value
