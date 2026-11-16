# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for the proof log's declared retention."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelLogRetentionRequest(BaseModel):
    """When the proof finished; the log is kept from then for the overlay's declared hours."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    finished_at: datetime

    @field_validator("finished_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("finished_at must be tz-aware")
        return value


class ModelLogRetentionResult(BaseModel):
    """How long the log is kept and the instant after which it may be removed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    log_hours: int = Field(..., ge=1)
    retain_until: datetime
