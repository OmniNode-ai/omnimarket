# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed record of one lane over the budget of its phase."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ModelWorkProgressStalled(BaseModel):
    """Published once per finding per evaluation, under the finding's key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    idempotency_key: str
    lane: str
    phase: str
    entering_row_id: str
    budget_seconds: int
    spent_seconds: int
    owner: str
    text: str
    correlation_id: UUID
