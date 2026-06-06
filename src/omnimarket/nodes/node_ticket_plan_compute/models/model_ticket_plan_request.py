# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input model for node_ticket_plan_compute."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelTicketPlanRequest(BaseModel):
    """Request to parse a plan document into ticket specs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_text: str = Field(description="Raw plan markdown text to parse")
    epic_id: str | None = Field(
        default=None,
        description="Optional Linear epic ID to attach tickets to",
    )
    team_id: str | None = Field(
        default=None,
        description="Optional Linear team ID for created tickets",
    )
