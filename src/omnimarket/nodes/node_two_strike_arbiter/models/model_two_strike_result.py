# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output event emitted by node_two_strike_arbiter."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumArbiterAction(StrEnum):
    """Action taken by the two-strike arbiter."""

    NO_ACTION = "no_action"
    FIRST_STRIKE = "first_strike"
    SECOND_STRIKE = "second_strike"
    DIAGNOSIS_WRITTEN = "diagnosis_written"
    TICKET_BLOCKED = "ticket_blocked"
    FRICTION_FILED = "friction_filed"


class ModelTwoStrikeResult(BaseModel):
    """Result produced by HandlerTwoStrikeArbiter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(..., description="Linear ticket identifier.")
    total_attempts: int = Field(..., description="Number of fix attempts seen.")
    action: EnumArbiterAction = Field(
        ...,
        description="Action taken by the arbiter.",
    )
    diagnosis_path: str | None = Field(
        default=None,
        description="Path to diagnosis markdown file, if written.",
    )
    friction_filed: bool = Field(
        default=False,
        description="Whether a friction event was recorded.",
    )
    dry_run: bool = Field(default=False, description="Whether this was a dry run.")
