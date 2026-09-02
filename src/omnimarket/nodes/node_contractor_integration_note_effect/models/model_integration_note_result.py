# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decision and result models for the contractor integration-note effect.

Two models, deliberately separate:

``ModelIntegrationNoteDecision`` is what the PURE composer returns. It answers
"is a note owed for this merge, and what does it say" with zero I/O, which is
what the unit tests exercise.

``ModelIntegrationNoteResult`` is what the EFFECT handler returns. It carries
the decision plus what actually happened at the Linear boundary. ``posted`` is
never inferred from ``should_post``: a dry run and a duplicate both leave it
False for different reasons, and the reason is recorded rather than implied.

Related:
    - OMN-17277: integration note (WS2)
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumNoteSkipReason(StrEnum):
    """Why no note is owed for a merge. Exhaustive by construction."""

    NO_TICKET_REFERENCE = "no_ticket_reference"
    TICKET_NOT_FOUND = "ticket_not_found"
    TICKET_UNASSIGNED = "ticket_unassigned"
    ASSIGNEE_NOT_CONTRACTOR = "assignee_not_contractor"
    ALREADY_POSTED = "already_posted"


class EnumReachability(StrEnum):
    """Whether the merged change is reachable from a released tag."""

    RELEASED = "released"
    DEV_ONLY = "dev_only"


class ModelIntegrationNoteDecision(BaseModel):
    """Pure composer output: whether a note is owed, and its exact body."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    should_post: bool = Field(..., description="True when a note is owed.")
    skip_reason: EnumNoteSkipReason | None = Field(
        default=None,
        description="Populated exactly when should_post is False.",
    )
    note_key: str = Field(
        ...,
        min_length=1,
        description=(
            "Idempotency key for this merge, e.g. 'OmniNode-ai/omnibase_infra#3120'. "
            "Present even on a skip so a caller can log what was suppressed."
        ),
    )
    note_body: str = Field(
        default="",
        description="The rendered note. Empty exactly when should_post is False.",
    )
    reachability: EnumReachability | None = Field(
        default=None,
        description="Released vs dev-only, when the merge was evaluated.",
    )
    recipient_display_name: str | None = Field(
        default=None, description="Contractor the note addresses, when matched."
    )
    ticket_identifier: str | None = Field(
        default=None, description="Ticket the note lands on, when resolved."
    )
    redacted_fields: tuple[str, ...] = Field(
        default=(),
        description=(
            "Field names withheld because their source text carried internal "
            "references (operator paths, lane names, session ids). Naming them "
            "is the point: a silently-emptied field reads as 'nothing to say'."
        ),
    )


class ModelIntegrationNoteResult(BaseModel):
    """Effect-handler output: the decision plus what happened at the boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(..., min_length=1, description="GitHub repo slug (owner/repo).")
    pr_number: int = Field(..., gt=0, description="Merged PR number.")
    decision: ModelIntegrationNoteDecision = Field(
        ..., description="The composer's verdict for this merge."
    )
    posted: bool = Field(
        ..., description="True only when a comment was actually written to Linear."
    )
    dry_run: bool = Field(
        ..., description="True when writing was suppressed by request."
    )


__all__ = [
    "EnumNoteSkipReason",
    "EnumReachability",
    "ModelIntegrationNoteDecision",
    "ModelIntegrationNoteResult",
]
