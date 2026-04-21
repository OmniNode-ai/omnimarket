# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input model for node_two_strike_arbiter."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelFixAttempt(BaseModel):
    """A single fix attempt record for a ticket/PR."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(..., description="Linear ticket identifier (e.g. OMN-1234).")
    repo: str = Field(..., description="Repository slug (e.g. omniclaude).")
    pr_number: int | None = Field(default=None, description="PR number if applicable.")
    branch: str | None = Field(default=None, description="Branch name if applicable.")
    attempt_number: int = Field(..., description="Which attempt this is (1-indexed).")
    error_summary: str = Field(..., description="Short summary of what went wrong.")
    error_detail: str = Field(default="", description="Full error detail / traceback.")
    attempted_at: str = Field(..., description="ISO-8601 timestamp of the attempt.")


class ModelTwoStrikeCommand(BaseModel):
    """Input command for the two-strike arbiter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(..., description="Linear ticket identifier.")
    repo: str = Field(default="", description="Repository slug, if known.")
    pr_number: int | None = Field(default=None, description="PR number, if applicable.")
    branch: str | None = Field(default=None, description="Branch name, if applicable.")
    fix_attempts: list[ModelFixAttempt] = Field(
        default_factory=list,
        description="Ordered list of fix attempts for this ticket.",
    )
    dry_run: bool = Field(default=False, description="Skip side effects when true.")
