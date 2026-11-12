# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deploy agent's dev-roll manifest event (OMN-24013, five whys R2)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelDevRollManifest(BaseModel):
    """The agent's dev-roll manifest; scope is the agent's enum (full, runtime, core)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID
    job_id: str = Field(min_length=1)
    issuer: str = Field(min_length=1)
    git_ref: str
    scope: str
    requested_at: datetime | None
    accepted_at: datetime
    open_pr: str
    rollback: bool
