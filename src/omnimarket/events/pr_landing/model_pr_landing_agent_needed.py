# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Payload of the agent-needed landing event."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.pr_landing.enum_pr_landing_agent_reason import (
    EnumPrLandingAgentReason,
)
from omnimarket.events.pr_landing.enum_pr_landing_state import (
    EnumPrLandingState,
)
from omnimarket.events.pr_landing.model_pr_landing_observation import (
    HEAD_SHA_PATTERN,
    REPOSITORY_PATTERN,
)


class ModelPrLandingAgentNeeded(BaseModel):
    """A PR only an agent can move. At most one per (head, reason) (P4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(..., pattern=REPOSITORY_PATTERN)
    pr_number: int = Field(..., ge=1)
    head_sha: str | None = Field(default=None, pattern=HEAD_SHA_PATTERN)
    reason: EnumPrLandingAgentReason = Field(...)
    from_state: EnumPrLandingState = Field(
        ..., description="The state the PR was in when the workflow gave up on it."
    )
    detail: str | None = Field(default=None, min_length=1)
    seq: int = Field(..., ge=0)
    raised_at: datetime = Field(...)


__all__: list[str] = ["ModelPrLandingAgentNeeded"]
