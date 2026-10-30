# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Types of the work-in-progress admission: the read, the limits, the request and the decision.

Work in progress is started-but-unlanded work: an open non-draft pull request, or a
ticket in a started state that a live CLAIM holds and that no counted pull request
already carries. It is counted per repository and for the whole fleet. Nothing here
names a repository, a host or an organization; the names arrive as data, and every
number arrives from the contract's ``config.wip_admission`` block or the
``wip_admission`` overlay.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumWipWorkKind(StrEnum):
    """What the caller asks to start."""

    NEW_WORK = "new_work"
    FIX_FORWARD = "fix_forward"
    TICKET_CREATE = "ticket_create"


class EnumWipFixClass(StrEnum):
    """The fix-forward classes. Which of them are admitted is the overlay's list."""

    RED = "red"
    REPEAT_BREAK = "repeat_break"
    LANDING_BLOCKER = "landing_blocker"


class EnumWipItemKind(StrEnum):
    PULL_REQUEST = "pull_request"
    TICKET_CLAIM = "ticket_claim"


class EnumWipOutcome(StrEnum):
    ADMIT = "admit"
    REFUSE = "refuse"


class EnumWipReason(StrEnum):
    TICKET_RECORD = "ticket_record"
    FIX_FORWARD = "fix_forward"
    FIX_FORWARD_READ_UNUSABLE = "fix_forward_read_unusable"
    UNDER_LIMIT = "under_limit"
    NOT_ENFORCING = "not_enforcing"
    AT_REPO_LIMIT = "at_repo_limit"
    AT_FLEET_LIMIT = "at_fleet_limit"
    READ_MISSING = "read_missing"
    READ_STALE = "read_stale"
    LIMIT_MISSING = "limit_missing"


class ModelWipItem(_Frozen):
    """One started-but-unlanded item the reader found.

    A pull request carries the tickets its title or branch cite; a ticket claim is
    only ever built for a ticket that is in a started state and has a live CLAIM.
    """

    kind: EnumWipItemKind
    repo: str
    ref: str = Field(min_length=1)
    tickets: tuple[str, ...] = ()
    draft: bool = False
    red: bool = False


class ModelWipRead(_Frozen):
    """The work in progress as read at ``read_at``; ``readable`` False means no count exists."""

    readable: bool
    read_at: datetime | None = None
    items: tuple[ModelWipItem, ...] = ()
    unreadable_reason: str = ""


class ModelWipCounts(_Frozen):
    """Started-but-unlanded counts: per repository and for the fleet."""

    by_repo: dict[str, int]
    fleet: int
    pull_requests: int
    ticket_claims: int


class ModelWipLimit(_Frozen):
    """One scope's limit with the measurement behind it (Little's law)."""

    limit: int = Field(ge=0)
    merges_per_hour: float = Field(ge=0)
    lead_time_hours: float = Field(gt=0)
    method: str = Field(min_length=1)
    evidence: str = Field(min_length=1)


class ModelWipPolicy(_Frozen):
    """What the node reads from the ``wip_admission`` overlay."""

    enforce_ruling: str
    fleet: ModelWipLimit
    limits: dict[str, ModelWipLimit]
    fix_forward_classes: tuple[EnumWipFixClass, ...]
    # Fill item kinds that are fix-forward by construction: the tick's own finding names
    # the red, so the fill never asks the gate about them.
    fix_forward_kinds: tuple[str, ...] = ()

    @property
    def enforcing(self) -> bool:
        return bool(self.enforce_ruling.strip())


class ModelWipAdmissionConfig(_Frozen):
    """The ``config.wip_admission`` block of the node contract."""

    limit_floor: int = Field(ge=0)
    reading_max_age_minutes: float = Field(gt=0)
    repeat_break_min: int = Field(ge=1)
    started_states: tuple[str, ...] = Field(min_length=1)
    ticket_pattern: str = Field(min_length=1)
    # Watcher classes: a held-back PR waits on a person or a hold, not on a lane, so it is
    # not work the fleet can land by starting less; a red one is what a fix-forward targets.
    excluded_classes: tuple[str, ...] = ()
    red_classes: tuple[str, ...] = Field(min_length=1)


class ModelWipFix(_Frozen):
    """What a fix-forward request offers as its evidence."""

    fix_class: EnumWipFixClass
    target: str = ""
    repeat_count: int = Field(default=0, ge=0)


class ModelWipAdmissionRequest(_Frozen):
    """One caller asking to start work.

    ``ticket`` is the caller's own ticket: its claim is left out of the count so a
    re-dispatch never blocks itself. A ticket-creating caller names no repository.
    """

    kind: EnumWipWorkKind
    repo: str = ""
    ticket: str = ""
    now: datetime
    read: ModelWipRead
    policy: ModelWipPolicy
    config: ModelWipAdmissionConfig
    fix: ModelWipFix | None = None


class ModelWipAdmissionDecision(_Frozen):
    """Admit or refuse, why, the counts judged and the printed line."""

    outcome: EnumWipOutcome
    reason: EnumWipReason
    kind: EnumWipWorkKind
    repo: str
    repo_count: int | None
    repo_limit: int | None
    fleet_count: int | None
    fleet_limit: int | None
    line: str


class ModelWipClaim(_Frozen):
    """A live CLAIM reduced to what the reader needs."""

    lane: str
    ticket: str
    repo: str = ""
