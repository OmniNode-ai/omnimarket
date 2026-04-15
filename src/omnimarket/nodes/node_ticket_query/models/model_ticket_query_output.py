# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

from pydantic import BaseModel, Field


class ModelIssueResult(BaseModel):
    """Wire-safe issue result — mirrors ProtocolProjectTracker ModelIssue fields."""

    model_config = {"frozen": True, "extra": "allow"}

    id: str
    identifier: str
    title: str
    description: str | None = None
    state: str
    priority: str | None = None
    assignee: str | None = None
    labels: tuple[str, ...] = Field(default=())
    team: str | None = None
    url: str | None = None


class ModelTicketQueryOutput(BaseModel):
    """Output from node_ticket_query."""

    model_config = {"frozen": True}

    issues: tuple[ModelIssueResult, ...] = Field(
        default=(),
        description="Issues returned by the query.",
    )
    total: int = Field(description="Number of issues returned.")
    query: str | None = Field(default=None, description="The query that was executed.")
    issue_id: str | None = Field(
        default=None, description="Single-issue fetch ID if used."
    )
