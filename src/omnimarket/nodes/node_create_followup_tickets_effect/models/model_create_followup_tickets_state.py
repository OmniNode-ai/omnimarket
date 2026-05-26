"""Models for node_create_followup_tickets_effect.

Input: list of review findings with severity, file paths, and descriptions.
Output: created ticket IDs, URLs, and any per-finding creation failures.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumFindingSeverity(StrEnum):
    """Severity levels that map to Linear priority values."""

    CRITICAL = "critical"  # Linear priority 1 — Urgent
    MAJOR = "major"  # Linear priority 2 — High
    MINOR = "minor"  # Linear priority 3 — Normal
    NIT = "nit"  # Linear priority 4 — Low


class ModelReviewFinding(BaseModel):
    """A single review finding to be converted into a Linear ticket."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    severity: EnumFindingSeverity = Field(
        ...,
        description="Severity level of the finding.",
    )
    description: str = Field(
        ...,
        description="Human-readable description of the finding.",
    )
    file_path: str | None = Field(
        default=None,
        description="Source file where the finding was identified.",
    )
    line_number: int | None = Field(
        default=None,
        description="Line number within the source file.",
    )
    keyword: str | None = Field(
        default=None,
        description="Keyword or category label from the review tool.",
    )


class ModelCreateFollowupTicketsCommand(BaseModel):
    """Command to create Linear tickets from a batch of review findings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(default="", description="Correlation ID for tracing.")
    source_review_id: str = Field(
        default="",
        description="Identifier of the review session that produced these findings.",
    )
    findings: tuple[ModelReviewFinding, ...] = Field(
        default=(),
        description="Ordered list of findings to create tickets for.",
    )
    project: str = Field(
        default="",
        description="Linear project name for fuzzy-match assignment.",
    )
    team: str = Field(
        default="Omninode",
        description="Linear team name.",
    )
    repo: str = Field(
        default="",
        description="Source repository label to attach as a ticket label.",
    )
    parent: str = Field(
        default="",
        description="Optional parent ticket ID (OMN-XXXX) for epic linkage.",
    )
    include_nits: bool = Field(
        default=False,
        description="When true, Nit-severity findings are included.",
    )
    dry_run: bool = Field(
        default=False,
        description="When true, generates ticket data but does not call Linear.",
    )


class ModelCreatedTicketRef(BaseModel):
    """Reference to a ticket that was successfully created."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_index: int = Field(
        ...,
        description="Zero-based index of the finding in the input list.",
    )
    ticket_id: str = Field(
        ...,
        description="Assigned Linear ticket ID (e.g., OMN-5957).",
    )
    ticket_url: str = Field(
        default="",
        description="URL of the created ticket.",
    )


class ModelTicketCreationFailure(BaseModel):
    """Record of a finding that could not be converted into a ticket."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_index: int = Field(
        ...,
        description="Zero-based index of the failing finding.",
    )
    reason: str = Field(
        ...,
        description="Error message or failure reason.",
    )


class ModelCreateFollowupTicketsResult(BaseModel):
    """Result of a batch ticket creation request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = Field(
        default="created",
        description="Overall status: created | dry_run | error | partial.",
    )
    correlation_id: str = Field(default="")
    created_tickets: tuple[ModelCreatedTicketRef, ...] = Field(
        default=(),
        description="Successfully created ticket references.",
    )
    failures: tuple[ModelTicketCreationFailure, ...] = Field(
        default=(),
        description="Findings that could not be converted to tickets.",
    )
    skipped_nit_count: int = Field(
        default=0,
        description="Number of Nit findings skipped due to include_nits=False.",
    )
    dry_run: bool = Field(default=False)


__all__: list[str] = [
    "EnumFindingSeverity",
    "ModelCreateFollowupTicketsCommand",
    "ModelCreateFollowupTicketsResult",
    "ModelCreatedTicketRef",
    "ModelReviewFinding",
    "ModelTicketCreationFailure",
]
