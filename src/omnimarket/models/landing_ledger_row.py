# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One parsed ledger row, shared by the landing nodes that derive facts from the rolling ledger.

Reading the ledger files is an effect; the nodes that derive the landing controller's facts take the
rows already parsed (``ts``, ``rtype``, ``lane``, ``text``).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

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
