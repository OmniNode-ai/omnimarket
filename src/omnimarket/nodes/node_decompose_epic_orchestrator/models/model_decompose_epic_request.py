# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for node_decompose_epic_orchestrator [OMN-12214].

Contains:
- ModelDecomposeEpicRequest: input to the orchestrator
- ModelCreatedSubTicket: a single created sub-ticket record
- ModelDecomposeEpicResult: output from the orchestrator
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelDecomposeEpicRequest(BaseModel):
    """Input to the decompose-epic orchestrator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    epic_id: str = Field(
        description="Linear epic ID to decompose (e.g. 'OMN-2000').",
    )
    max_tickets: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Maximum number of sub-tickets to generate. Capped at 50 to prevent runaway creation.",
    )
    generate_contracts: bool = Field(
        default=True,
        description=(
            "When True, generate OCC contract YAML stubs for each created ticket "
            "and open a single PR against onex_change_control."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "When True, analyze and plan the decomposition but do not create tickets "
            "or emit any downstream commands. Returns status 'dry_run'."
        ),
    )
    correlation_id: UUID = Field(
        description="Correlation ID for tracing this orchestration run.",
    )


class ModelCreatedSubTicket(BaseModel):
    """Record for a single sub-ticket created by the orchestrator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(description="Linear ticket ID (e.g. 'OMN-2001').")
    title: str = Field(description="Ticket title.")
    repo_hint: str = Field(description="Owning repo inferred from epic keywords.")
    linear_id: str = Field(
        description="Linear internal UUID for the ticket (used for parent linking)."
    )


class ModelDecomposeEpicResult(BaseModel):
    """Output from the decompose-epic orchestrator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    epic_id: str = Field(description="The source epic that was decomposed.")
    status: str = Field(
        description="Outcome: 'success', 'dry_run', or 'error'.",
    )
    created_tickets: tuple[ModelCreatedSubTicket, ...] = Field(
        default=(),
        description="Sub-tickets created during this run (empty on dry_run or error).",
    )
    contract_files_generated: tuple[str, ...] = Field(
        default=(),
        description=(
            "Relative paths (within onex_change_control) of OCC contract stubs "
            "generated for this decomposition. Empty when generate_contracts=False "
            "or dry_run=True."
        ),
    )
    contract_pr_url: str | None = Field(
        default=None,
        description="URL of the PR opened against onex_change_control, if created.",
    )
    correlation_id: UUID = Field(description="Correlation ID echoed from the request.")

    @property
    def count(self) -> int:
        """Number of sub-tickets created."""
        return len(self.created_tickets)
