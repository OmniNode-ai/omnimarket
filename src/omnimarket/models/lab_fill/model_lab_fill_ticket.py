# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ticket-backlog facts and plan for idle lab slots (OMN-20864).

Operator ruling 2026-10-20T10:45:21Z: lab-fill rules live in omnimarket; the plugin
only supplies facts and calls them. These frozen contracts carry every input and
every admitted or dropped ticket without file, network or clock reads.
"""

from __future__ import annotations

from collections import Counter
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class EnumLabFillTicketDrop(StrEnum):
    """The first matching gate that excludes a ticket from lab-fill."""

    CLOSED = "closed"
    OTHER_OWNER = "other-owner"
    NOT_PILLAR = "not-pillar"
    OUT_OF_SCOPE = "out-of-scope"
    DASHBOARD = "dashboard"
    OPEN_PR = "open-pr"
    CLAIMED = "claimed"
    NO_CRITERIA = "no-criteria"


class EnumLabFillTicketSource(StrEnum):
    """Eligible ticket sources, in dispatch priority order."""

    M4_CARRIER = "m4-carrier"
    DELEGATION = "delegation"
    SPRINT_CURRENT = "sprint-current"
    SPRINT_NEXT = "sprint-next"


class ModelLabFillTicketIssue(BaseModel):
    """One ticket as observed by the caller, including sprint acceptance criteria."""

    model_config = _FROZEN

    id: str = Field(pattern=r"^OMN-[0-9]+$")
    title: str = ""
    status: str = ""
    status_type: str = ""
    assignee: str | None = None
    labels: tuple[str, ...] = ()
    project: str = ""
    repo: str = ""
    pillar: str | None = None
    description: str = ""


class ModelLabFillTicketFacts(BaseModel):
    """All backlog inputs; the caller supplies today's date and ownership facts."""

    model_config = _FROZEN

    today: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    operator: str | None = None
    issues: tuple[ModelLabFillTicketIssue, ...] = ()
    m4_carriers: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    claimed: tuple[str, ...] = ()
    open_pr: tuple[str, ...] = ()
    out_of_scope: tuple[str, ...] = ()


class ModelLabFillTicketItem(BaseModel):
    """An admitted ticket with its source and unchecked labelled criteria."""

    model_config = _FROZEN

    id: str
    title: str
    repo: str
    status: str
    source: EnumLabFillTicketSource
    carriers: tuple[str, ...] = ()
    project: str = ""
    criteria: tuple[str, ...] = ()


class ModelLabFillTicketDrop(BaseModel):
    """A ticket's first exclusion reason, with context for the operator."""

    model_config = _FROZEN

    id: str
    reason: EnumLabFillTicketDrop
    detail: str = ""


class ModelLabFillTicketPlan(BaseModel):
    """Source-ranked tickets and input-ordered drops; no ticket is silently lost."""

    model_config = _FROZEN

    items: tuple[ModelLabFillTicketItem, ...] = ()
    drops: tuple[ModelLabFillTicketDrop, ...] = ()

    def drop_counts(self) -> dict[str, int]:
        """Count drops by reason value, sorted by key for stable diagnostics."""
        return dict(sorted(Counter(drop.reason.value for drop in self.drops).items()))
