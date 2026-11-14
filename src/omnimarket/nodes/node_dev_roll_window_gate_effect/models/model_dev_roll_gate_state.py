# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gate's whole durable state: the held commands and the last roll it released."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.events.runtime_deployment import ModelDeployPublishCommand
from omnimarket.models.dev_roll import ModelDevRollRecord


class ModelHeldDevRoll(BaseModel):
    """One dev deploy command waiting for its window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: ModelDeployPublishCommand
    repository: str = Field(min_length=1)
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    held_at: datetime

    @field_validator("held_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("held_at must carry a UTC offset")
        return value


class ModelDevRollGateState(BaseModel):
    """Held commands (oldest first), the last released roll, and the released ids kept."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    held: tuple[ModelHeldDevRoll, ...] = ()
    last_roll: ModelDevRollRecord | None = None
    released_ids: tuple[str, ...] = ()
