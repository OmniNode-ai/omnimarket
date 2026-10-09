# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ledger rows one landing tick reads, and the facts derived from them.

The rows are already parsed (``ts``, ``rtype``, ``lane``, ``text``): reading the ledger files is an
effect, deriving the landing controller's ledger facts from the rows is this node's compute.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.models.landing_decision import (
    ModelLandingCauseOwner,
    ModelLandingCauseRelease,
)
from omnimarket.models.landing_ledger_row import ModelLandingLedgerRow


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


class ModelLandingCauseFix(BaseModel):
    """A fix PR an open cause CLAIM declares in its fix= cell."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr: str
    lane: str
    cause: str


class ModelLandingLedgerFacts(BaseModel):
    """The ledger-derived facts the landing controller reads that need only the rows."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_read: int = Field(description="How many window rows were read.")
    fixer_hold: tuple[str, ...] = Field(
        description="Repos (or 'all') under the fixer kill switch."
    )
    cause_owners: tuple[ModelLandingCauseOwner, ...]
    cause_releases: tuple[ModelLandingCauseRelease, ...]
    cause_fixes: tuple[ModelLandingCauseFix, ...]
    lab_passes: dict[str, tuple[str, ...]] = Field(
        description="repo#n -> every head a lab proof PASS readback recorded."
    )
    drain_lane: str = Field(description="The live repo drain lane, or ''.")


__all__: list[str] = [
    "ModelLandingCauseFix",
    "ModelLandingLedgerFacts",
    "ModelLandingLedgerRow",
    "ModelLandingLedgerRows",
]
