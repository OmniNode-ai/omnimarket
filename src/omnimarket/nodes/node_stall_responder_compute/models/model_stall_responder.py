# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the stall responder (OMN-22885)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelStallResponderRequest(BaseModel):
    """Ledger rows to read, and what the responder already did.

    rows: ledger lines, any types; only the throughput tick's THROUGHPUT STALL MSG rows count.
    dispatched: cause key -> stamp of the row that last dispatched it, from the responder's state.
    covered_lanes: names of lanes already live; a lane of the class's own prefix naming the
    cause covers it.
    redispatch_after_hours: a key dispatched this long ago, and still named by a stall row,
    dispatches again.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[str, ...]
    dispatched: dict[str, str] = Field(default_factory=dict)
    covered_lanes: tuple[str, ...] = ()
    redispatch_after_hours: float = Field(default=24.0, gt=0)


class ModelCauseDispatch(BaseModel):
    """One cause lane to start: the cause key, its class, and the first stall row that named it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cause_key: str
    cause_class: str
    subject: str
    lane: str
    ticket: str
    first_stamp: str
    occurrences: int


class ModelCauseSkip(BaseModel):
    """A cause key that starts nothing, with the reason: already-dispatched, covered, tick-owned."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cause_key: str
    reason: str
    occurrences: int


class ModelRowRefusal(BaseModel):
    """A stall row the responder refused to read, with the row's stamp and the typed reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stamp: str
    reason: str


class ModelStallResponderResult(BaseModel):
    """Dispatches (one per distinct cause key), skips, the updated dispatched map and counts.

    refusals: stall rows whose stamp is not a calendar date (a shape-valid 2026-11-31T00:00:00Z);
    each is refused alone and the rest of the batch is answered.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    dispatches: tuple[ModelCauseDispatch, ...]
    skipped: tuple[ModelCauseSkip, ...]
    dispatched: dict[str, str]
    stall_rows: int
    unreadable_rows: int
    truncated_findings: int
    refusals: tuple[ModelRowRefusal, ...] = ()
