# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The last roll, as the deploy agent's job record and health read give it."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from omnimarket.models.dev_roll.enum_dev_roll_health import (
    EnumDevRollHealth,
)


class ModelDevRollRecord(BaseModel):
    """One roll: when its runtime started, whether it finished, and its health."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str
    git_ref: str
    runtime_started_at: datetime | None
    finished_at: datetime | None
    health: EnumDevRollHealth
    healthy_since: datetime | None

    @field_validator("runtime_started_at", "finished_at", "healthy_since")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("a roll time must carry a UTC offset")
        return value

    @model_validator(mode="after")
    def _healthy_since_needs_a_healthy_finished_roll(self) -> ModelDevRollRecord:
        if self.healthy_since is not None and (
            self.health is not EnumDevRollHealth.HEALTHY or self.finished_at is None
        ):
            raise ValueError("healthy_since needs a finished roll read healthy")
        return self
