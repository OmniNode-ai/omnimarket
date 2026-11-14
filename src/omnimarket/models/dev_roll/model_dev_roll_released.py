# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The held dev deploy commands the gate releases together, oldest first."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.events.runtime_deployment import ModelDeployPublishCommand


class ModelDevRollReleased(BaseModel):
    """One release: every command the batching decision rolled, oldest first.

    Topic ``onex.evt.omnimarket.dev-roll-released.v1``. The deploy effect publishes each
    command to the deploy agent in this order and does not send any of them back to the
    gate: a released command is never held a second time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    release_id: UUID
    decision_correlation_id: UUID | None
    released_at: datetime
    commands: tuple[ModelDeployPublishCommand, ...] = Field(min_length=1)

    @field_validator("released_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("released_at must carry a UTC offset")
        return value
