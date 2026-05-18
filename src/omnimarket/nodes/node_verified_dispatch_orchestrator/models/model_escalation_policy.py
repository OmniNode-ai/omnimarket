# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Escalation policy for the verified dispatch orchestrator.

Related:
    - OMN-11220: Verification-First Parallel Worker Dispatch Skill
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelEscalationPolicy(BaseModel):
    """Policy governing retry and escalation behaviour when a verifier rejects a worker."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_attempts: int = Field(
        default=3,
        ge=1,
        description="Maximum number of worker+verifier attempts before escalation.",
    )
    cooldown_seconds: int = Field(
        default=60,
        ge=0,
        description="Seconds to wait between retry attempts.",
    )
    escalation_action: Literal["linear_ticket", "human_review"] = Field(
        default="linear_ticket",
        description="Action taken when max_attempts is exhausted without acceptance.",
    )


__all__: list[str] = ["ModelEscalationPolicy"]
