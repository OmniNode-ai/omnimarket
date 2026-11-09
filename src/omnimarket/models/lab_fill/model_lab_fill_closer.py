# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A lab-fill ticket whose work is already built goes to the closer, not to a lane (OMN-23376).

The fill dispatched about 20 fill-ticket lanes every half hour, and most ended skipped-owned, skipped-already-built or done with
no change because the ticket's work was merged and only its Linear boxes were unticked. Each such lane built a worktree and a
virtual environment first. A merged change that names the ticket, or a lane's own already-built report with passing falsifiers,
now routes the ticket to the closer with its evidence, and the fill does not dispatch it again.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class EnumLabFillCloserReason(StrEnum):
    """Why a ticket goes to the closer instead of a lane."""

    IMPLEMENTATION_MERGED = "implementation-merged"
    ALREADY_BUILT = "already-built"


class ModelLabFillBuiltReport(BaseModel):
    """One lane's own TERMINAL that reported a ticket already built, read from the ledger."""

    model_config = _FROZEN

    ticket: str = Field(pattern=r"^OMN-[0-9]+$")
    lane: str
    reported_at: str
    # The lane ran every acceptance falsifier of the ticket and each passed (the TERMINAL's falsifiers=pass cell).
    falsifiers_passed: bool = False
    evidence: str = ""


class ModelLabFillCloserRoute(BaseModel):
    """A ticket the fill hands to the closer, with the evidence it holds; the closer closes on evidence, never on a merge alone."""

    model_config = _FROZEN

    ticket: str
    reason: EnumLabFillCloserReason
    # The merged pull requests (repo#number, or repo@sha for a merge with no number) that name the ticket.
    prs: tuple[str, ...] = ()
    # The lane that reported the ticket already built, and what it ran.
    lane: str = ""
    evidence: str = ""
    closer: str
