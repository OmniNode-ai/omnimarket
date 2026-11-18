# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ledger text and open pull requests in, the fence list out (OMN-20682).

The verdict's field names and order are the ones the report carries after ``now`` and ``ledger``
(the deleted ``triage_fences.build_report`` put them there), so a report dumps to the JSON the
triage lanes already read.
"""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelTriageFencesOpenPull(_Frozen):
    """One open pull request as `gh pr list` returns it."""

    repo: str
    number: int | None = None
    title: str = ""
    head_ref_name: str = ""
    updated_at: str = ""
    url: str | None = None


class ModelTriageFencesClaim(_Frozen):
    last_activity_ts: str
    lane: str | None
    tickets: tuple[str, ...]
    related: tuple[str, ...]
    claim_ts: str
    last_update_ts: str
    update_count: int
    age_hours: float
    actor: str | None
    source: str
    form: str
    # Why the claim reads live: ``running-receipt`` (the lane's runner receipt says running) or
    # ``recent-row`` (the lane wrote a ledger row inside the window). Empty on a stale claim.
    live_basis: str = ""


class ModelTriageFencesPullFence(_Frozen):
    ticket: str
    repo: str
    number: int | None
    title: str
    branch: str
    url: str | None
    updated_at: str | None
    age_hours: float | None
    within_window: bool
    matched_on: str


class ModelTriageFencesCounts(_Frozen):
    rows_parsed: int
    open_claims: int
    stale_claims: int
    open_pr_fences: int
    fenced_tickets: int
    unparsed_rows: int


class ModelTriageFencesDeriveRequest(_Frozen):
    """Every input of one derivation; the handler reads no file and no clock."""

    ledger_name: str
    ledger_text: str
    now: AwareDatetime
    stale_after_hours: float = Field(gt=0)
    pr_window_hours: float = Field(gt=0)
    open_pulls: tuple[ModelTriageFencesOpenPull, ...] = ()
    # Lanes whose runner receipt reads running now, supplied by the effect that read the receipts.
    # A claim of a running lane stays live however long its lane has written no ledger row.
    running_lanes: tuple[str, ...] = ()


class ModelTriageFencesVerdict(_Frozen):
    fenced_tickets: tuple[str, ...]
    open_claims: tuple[ModelTriageFencesClaim, ...]
    stale_claims: tuple[ModelTriageFencesClaim, ...]
    open_pr_fences: tuple[ModelTriageFencesPullFence, ...]
    unparsed_rows: tuple[str, ...]
    counts: ModelTriageFencesCounts
