# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed item record, owner step and unowned reading of node_item_owner_dispatch_effect (OMN-25981).

An item is a fold over ledger rows (no second store): a lane TERMINAL that ended blocked or partial with no successor
CLAIM, a RULING whose mechanism is pending, a MISSING verdict of the plan owner check, or an operator-capture ask.
The vocabulary is the ask-state fold's: ``open`` (no running lane), ``owned`` (a running lane) and ``done``, with the
accountable owner (the lane that last owned it) kept apart from the running lane (the lane with a live CLAIM now).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumItemSource(StrEnum):
    BLOCKED_LANE = "blocked-lane"
    RULING = "ruling"
    PLAN_OWNER = "plan-owner"
    OPERATOR_CAPTURE = "operator-capture"


class EnumItemState(StrEnum):
    OPEN = "open"
    OWNED = "owned"
    DONE = "done"


class EnumOwnerSkip(StrEnum):
    """Why the owner step placed no lane for an item this pass. A skip is never a verdict on the item's work."""

    DONE = "done"
    RUNNING = "running"
    YOUNG = "younger-than-bound"
    RESERVED = "reserved-by-another-run"
    CAP = "over-tick-cap"
    BRIEF_CHECK_FAILED = "brief-check-failed"
    PLACEMENT_FAILED = "placement-failed"
    DRY_RUN = "dry-run"


class EnumReadingVerdict(StrEnum):
    OK = "OK"
    FINDING = "FINDING"
    UNREAD = "UNREAD"


class ModelItemRecord(_Frozen):
    """One item: deterministic id, source, first-seen time, revision, accountable owner and running lane."""

    item_id: str = Field(pattern=r"^(?:itm|cap)-[0-9a-f]{10}$")
    source: EnumItemSource
    #: The source row's stamp and lane (or the plan rule), what the id was hashed from.
    source_ref: str = Field(min_length=1)
    first_seen_at: str
    #: The item's content digest; a transition of an older revision is ignored (the fence of plan task 3.3).
    revision: str = Field(pattern=r"^[0-9a-f]{12}$")
    state: EnumItemState
    accountable_owner: str | None = None
    running_lane: str | None = None
    #: How many owner bindings this item has had at its revision; the next binding is ``generation + 1``.
    generation: int = Field(ge=0)
    #: Since when the item has had no running lane (its first-seen time, or the end of its last binding).
    unowned_since: str
    ticket: str | None = None
    pr: str | None = None
    summary: str = ""


class ModelItemFold(_Frozen):
    items: tuple[ModelItemRecord, ...]
    rows_read: int = Field(ge=0)


class ModelOwnerDue(_Frozen):
    """An item past its bound with no running lane, and the deterministic owner lane for its next binding."""

    item: ModelItemRecord
    lane: str = Field(min_length=1)
    generation: int = Field(ge=1)
    #: The reservation key: one owner lane per (item, revision, generation), whatever run or worker asks.
    reservation_key: str = Field(min_length=1)


class ModelOwnerPlan(_Frozen):
    items: tuple[ModelItemRecord, ...]
    due: tuple[ModelOwnerDue, ...]
    skipped: tuple[tuple[str, EnumOwnerSkip], ...]


class ModelOwnerPlaced(_Frozen):
    item_id: str
    lane: str
    generation: int
    started: bool
    brief_path: str = ""
    brief_sha256: str = ""
    owned_row: str = ""
    skip: EnumOwnerSkip | None = None
    detail: str = ""


class ModelItemOwnerRequest(_Frozen):
    #: The run that asks (a Temporal workflow id/run id, or a by-hand id); written on the ``state=owned`` row.
    run_id: str = Field(min_length=1)
    dry_run: bool = False


class ModelItemOwnerResult(_Frozen):
    run_id: str
    items: tuple[ModelItemRecord, ...]
    placed: tuple[ModelOwnerPlaced, ...]
    skipped: tuple[tuple[str, EnumOwnerSkip], ...]


class ModelUnownedReading(_Frozen):
    verdict: EnumReadingVerdict
    read_at: str
    bound_minutes: float
    items_read: int = Field(ge=0)
    unowned: tuple[str, ...] = ()
    detail: str = ""
    status_row: str = ""


__all__ = [
    "EnumItemSource",
    "EnumItemState",
    "EnumOwnerSkip",
    "EnumReadingVerdict",
    "ModelItemFold",
    "ModelItemOwnerRequest",
    "ModelItemOwnerResult",
    "ModelItemRecord",
    "ModelOwnerDue",
    "ModelOwnerPlaced",
    "ModelOwnerPlan",
    "ModelUnownedReading",
]
