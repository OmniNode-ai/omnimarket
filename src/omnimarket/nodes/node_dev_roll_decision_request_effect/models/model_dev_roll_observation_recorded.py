# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The output of the decided-observed handler."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelDevRollObservationRecorded(BaseModel):
    """Whether one observed decision changed the observation state, and how."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    action: str
    cleared_count: int = Field(ge=0)
    recorded: bool
    error: str | None
