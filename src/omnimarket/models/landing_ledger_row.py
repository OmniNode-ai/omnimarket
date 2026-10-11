# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One parsed ledger row, shared by the landing nodes that derive facts from the rolling ledger.

Reading the ledger files is an effect; the nodes that derive the landing controller's facts take the
rows already parsed (``ts``, ``rtype``, ``lane``, ``text``).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

STAMP_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z$"


class ModelLandingLedgerRow(BaseModel):
    """One ledger row: the first line opens it, later lines of the same row follow after a newline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: str = Field(pattern=STAMP_PATTERN)
    rtype: str = Field(
        min_length=1, description="The row's canonical type, or '?' when unknown."
    )
    lane: str | None = None
    text: str


__all__ = ["STAMP_PATTERN", "ModelLandingLedgerRow"]


class ModelLandingLedgerRows(BaseModel):
    """The ledger rows of one tick: the coordination window, and older PASS readbacks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: datetime = Field(description="The tick's clock, timezone-aware.")
    window_days: float = Field(
        default=7.0, gt=0, description="A CLAIM older than this owns nothing."
    )
    rows: tuple[ModelLandingLedgerRow, ...] = Field(
        description="The window's rows in ledger order, none newer than now."
    )
    history_rows: tuple[ModelLandingLedgerRow, ...] = Field(
        default=(),
        description="Rows of older ledger rolls; only their lab PASS readbacks are read.",
    )

    @field_validator("now")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return value
