# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tier-1 candidate sources: the stalled-plan and pending-ruling tickets (OMN-20668).

The effect side runs the two tools and reads Linear and the PR watcher; these models carry
what it read, or why it read nothing, so every source decision stays pure here.
"""

from pydantic import BaseModel, ConfigDict, Field, JsonValue

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ModelLabFillSourceRead(BaseModel):
    """One tool's printed JSON document, or the effect's reason it has none."""

    model_config = _FROZEN
    document: JsonValue = None
    error: str = ""


class ModelLabFillSourceTicketsRequest(BaseModel):
    """A source left out (None) is not configured; its read stays empty."""

    model_config = _FROZEN
    plan_priorities: tuple[str, ...]
    plan_action_kinds: tuple[str, ...]
    plans: ModelLabFillSourceRead | None = None
    rulings: ModelLabFillSourceRead | None = None


class ModelLabFillSourceTicket(BaseModel):
    """One ticket a source named, with the evidence line the lane brief carries."""

    model_config = _FROZEN
    kind: str
    ticket: str
    evidence: str


class ModelLabFillSourceDiagnostic(BaseModel):
    """A source's read (empty, ok or unreadable:<detail>) and what it dropped, by reason."""

    model_config = _FROZEN
    read: str
    dropped: int = 0
    reasons: dict[str, int] = Field(default_factory=dict)


class ModelLabFillSourceTicketsResult(BaseModel):
    """Every source in order, the tickets of the sources read ok, and the tickets to read."""

    model_config = _FROZEN
    sources: dict[str, ModelLabFillSourceDiagnostic]
    found: tuple[ModelLabFillSourceTicket, ...]
    wanted: tuple[str, ...]


class ModelLabFillSourceAttachment(BaseModel):
    """A Linear attachment; a GitHub pull request URL names an attached PR."""

    model_config = _FROZEN
    url: str = ""
    created_at: str = ""


class ModelLabFillSourceTicketFacts(BaseModel):
    """What Linear holds for a wanted ticket; the declared repository is read beside it."""

    model_config = _FROZEN
    identifier: str
    title: str
    updated_at: str
    state_type: str
    has_children: bool = False
    labels: tuple[str, ...] = ()
    project_id: str = ""
    assignee_id: str = ""
    attachments: tuple[ModelLabFillSourceAttachment, ...] = ()
    declared_repo: str = ""


class ModelLabFillSourceOpenPr(BaseModel):
    """One PR watcher record: its key, short repository name, state and title."""

    model_config = _FROZEN
    key: str
    repo: str = ""
    state: str = ""
    title: str = ""


class ModelLabFillSourceCandidatesRequest(BaseModel):
    """A ticket or watcher read that failed names every read source unreadable."""

    model_config = _FROZEN
    sources: ModelLabFillSourceTicketsResult
    tickets: tuple[ModelLabFillSourceTicketFacts, ...] = ()
    open_prs: tuple[ModelLabFillSourceOpenPr, ...] = ()
    read_error: str = ""


class ModelLabFillSourceCandidate(BaseModel):
    """A tier-1 candidate in the shape the selection reads."""

    model_config = _FROZEN
    kind: str
    ticket: str
    title: str
    labels: tuple[str, ...]
    area: str = ""
    milestone: str = ""
    under_parent: bool = False
    updated_at: str
    project_id: str
    assignee_id: str
    ticket_prs: tuple[str, ...]
    declared_repo: str
    self_contained_code: bool = False
    evidence: str


class ModelLabFillSourceCandidatesResult(BaseModel):
    """The sources' final reads and drops, and the candidates that survived."""

    model_config = _FROZEN
    sources: dict[str, ModelLabFillSourceDiagnostic]
    candidates: tuple[ModelLabFillSourceCandidate, ...]
