# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelTicketResearchEnrichmentRequest — input to the research enrichment compute node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelTicketResearchEnrichmentRequest(BaseModel):
    """Request to enrich the research phase with knowledge context."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(..., description="Linear ticket ID (e.g., OMN-11941)")
    repo: str = Field(..., description="Target repo slug (e.g., omnimarket)")
    description: str = Field(default="", description="Ticket description text")
    linked_files: tuple[str, ...] = Field(
        default=(),
        description="Repo-relative file paths linked to the ticket",
    )
    context_timeout_s: float = Field(
        default=10.0,
        description="Max seconds to wait for context assembler; research proceeds on timeout",
        gt=0.0,
    )


__all__: list[str] = ["ModelTicketResearchEnrichmentRequest"]
