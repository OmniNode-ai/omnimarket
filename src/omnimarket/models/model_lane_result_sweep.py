# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the lane result sweep (OMN-22967): one verdict per lane receipt.

A lane receipt is read from the lane's TERMINAL row. The row's ``outcome`` cell is what the lane
(or the hand-off) claimed; ``lane_result`` / ``result``, ``blocker`` and ``engine_rc`` are what the
lane actually returned. The sweep classifies by the worse of the two, so a hand-off that
stopped on a blocker reads blocked and is never counted done.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict


class EnumLaneResultClass(StrEnum):
    """What a lane's receipt really was, after the claimed outcome is checked against its result."""

    DONE = "done"
    HANDED_OFF = "handed-off"
    SKIPPED_OWNED = "skipped-owned"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    FAILED = "failed"
    UNKNOWN = "unknown"

    @property
    def needs_action(self) -> bool:
        """A class that is not a clean finish needs an action row or a valid park."""
        return self not in (
            EnumLaneResultClass.DONE,
            EnumLaneResultClass.HANDED_OFF,
            EnumLaneResultClass.SKIPPED_OWNED,
        )


class EnumSweepState(StrEnum):
    """Where one receipt stands against the one-hour rule."""

    CLEAR = "clear"  # a clean finish: classified, nothing to act on
    ACTED = "acted"  # an action row exists
    PARKED = "parked"  # a valid, unexpired park exists
    PENDING = "pending"  # classified, still inside the hour
    NO_ACTION_AT_ONE_HOUR = (
        "no-action-at-one-hour"  # class row, no action, no valid park
    )
    PARK_EXPIRED = "park-expired"  # a park exists and its review_by has passed
    UNCLASSIFIED_LATE = "unclassified-late"  # no class row until after the hour

    @property
    def fails(self) -> bool:
        return self in (
            EnumSweepState.NO_ACTION_AT_ONE_HOUR,
            EnumSweepState.PARK_EXPIRED,
            EnumSweepState.UNCLASSIFIED_LATE,
        )


class ModelLaneReceipt(BaseModel):
    """One lane's TERMINAL as the sweep reads it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    ticket: str = ""
    finished_at: datetime
    #: The TERMINAL row's ``outcome`` cell: what was claimed.
    terminal_outcome: str = Field(min_length=1)
    #: The lane's own result (``lane_result`` or ``result`` cell), when the row carries it.
    result_outcome: str = ""
    blocker: str = ""
    engine_rc: int | None = None
    #: Free text the error rules read: the blocker, else the row's closing text.
    error_text: str = ""

    @property
    def receipt_key(self) -> str:
        return f"{self.lane}@{self.finished_at.strftime('%Y-%m-%dT%H:%M:%SZ')}"


class ModelReceiptVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    ticket: str
    receipt_key: str
    terminal_outcome: str
    effective_class: EnumLaneResultClass
    state: EnumSweepState
    class_key: str = ""
    rule_id: str = ""
    reasons: tuple[str, ...] = ()


class ModelLaneResultSweepCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    #: Receipts finished at or after this time are swept.
    since: datetime
    #: Append the class, action and reading rows; false only reads and judges.
    write: bool = False


class ModelLaneResultSweepResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    status: str  # "completed" | "failed"
    verdict: EnumReadingVerdict | None = None
    outcome: EnumReadingOutcome | None = None
    reasons: tuple[str, ...] = ()
    verdicts: tuple[ModelReceiptVerdict, ...] = ()
    rows_written: tuple[str, ...] = ()
    failure: str = ""


__all__ = [
    "EnumLaneResultClass",
    "EnumSweepState",
    "ModelLaneReceipt",
    "ModelLaneResultSweepCommand",
    "ModelLaneResultSweepResult",
    "ModelReceiptVerdict",
]
