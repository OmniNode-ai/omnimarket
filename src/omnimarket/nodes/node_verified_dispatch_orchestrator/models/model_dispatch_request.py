# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input model for the verified dispatch orchestrator node.

Related:
    - OMN-11220: Verification-First Parallel Worker Dispatch Skill
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelDispatchRequest(BaseModel):
    """Input to the verified dispatch orchestrator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(..., description="Linear ticket ID (e.g. OMN-1234).")
    worker_prompt: str = Field(
        ..., description="Prompt or task description passed to the worker subagent."
    )
    max_attempts: int = Field(
        default=3, ge=1, description="Maximum retry attempts before escalation."
    )
    cooldown_seconds: int = Field(
        default=60, ge=0, description="Cooldown between retry attempts in seconds."
    )
    escalation_action: Literal["linear_ticket", "human_review"] = Field(
        default="linear_ticket",
        description="Action on max-attempts exhaustion.",
    )
    correlation_id: str | None = Field(
        default=None,
        description="Correlation ID for tracing across worker and verifier runs.",
    )


__all__: list[str] = ["ModelDispatchRequest"]
