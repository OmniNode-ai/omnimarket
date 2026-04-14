# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

from pydantic import BaseModel, Field


class ModelTicketQueryInput(BaseModel):
    """Input for node_ticket_query."""

    model_config = {"frozen": True}

    query: str | None = Field(
        default=None,
        description="Free-text search query (e.g. 'OMN-8771', 'auth middleware'). "
        "If None, list_issues is called with filters only.",
    )
    filters: dict[str, str] | None = Field(
        default=None,
        description="Optional key-value filters: state, assignee, label, team, etc.",
    )
    limit: int = Field(default=50, ge=1, le=250, description="Max results to return.")
    issue_id: str | None = Field(
        default=None,
        description="If set, fetch a single issue by ID instead of searching.",
    )
