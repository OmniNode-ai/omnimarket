# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output model for alert classification + dispatch."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AlertTier = Literal["RECOVERABLE", "UNKNOWN", "ESCALATE"]


class ModelAlertResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    alert_id: str = Field(..., description="Echoed from input event")
    tier: AlertTier = Field(..., description="Classification result")
    playbook_id: str | None = Field(
        default=None, description="Matched playbook ID (RECOVERABLE only)"
    )
    dispatch_prompt: str | None = Field(
        default=None, description="Rendered remediation prompt (RECOVERABLE only)"
    )
    linear_ticket_id: str | None = Field(
        default=None, description="Auto-created ticket (ESCALATE only)"
    )
    notes: str = Field(default="", description="Human-readable outcome summary")
