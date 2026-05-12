# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelSemanticGradingRequest -- input contract for node_pr_semantic_grader_llm_effect."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelSemanticGradingRequest(BaseModel):
    """Command payload for a single PR semantic grading call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(
        ...,
        description="Ticket identifier (e.g. 'OMN-10834').",
    )
    acceptance_criteria: list[str] = Field(
        ...,
        description="Acceptance criteria statements from ModelTicketContract.requirements[*].acceptance[*].statement.",
    )
    pr_diff_text: str = Field(
        ...,
        description="Unified diff text from `gh pr diff <num>`.",
    )
    pr_title: str = Field(
        ...,
        description="Pull request title.",
    )
    correlation_id: str = Field(
        ...,
        description="Correlation ID linking this grading call to the triggering event.",
    )


__all__: list[str] = ["ModelSemanticGradingRequest"]
