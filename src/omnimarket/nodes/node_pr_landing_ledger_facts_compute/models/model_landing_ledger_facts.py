# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ledger rows one landing tick reads, and the facts derived from them.

The rows are already parsed (``ts``, ``rtype``, ``lane``, ``text``): reading the ledger files is an
effect, deriving the landing controller's ledger facts from the rows is this node's compute.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.landing_decision import (
    ModelLandingCauseOwner,
    ModelLandingCauseRelease,
)
from omnimarket.models.landing_ledger_row import (
    ModelLandingLedgerRow,
    ModelLandingLedgerRows,
)


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
