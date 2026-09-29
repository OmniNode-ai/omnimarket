# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Best graded, non-truncated answer retained across escalation."""

from pydantic import BaseModel, ConfigDict, Field


class ModelAnsweredDraft(BaseModel):
    """Durable content and its exact escalation-history source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str = Field(min_length=1)
    gate_score: float
    attempt_index: int = Field(ge=0)
    tier: str
    backend_ref: str | None
