# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Fold request: one row event as it arrives off the bus (OMN-19513).

``raw_row`` is the truth for the typed view, re-derived by the same pure parser
the emit node uses. Besides ``raw_row``, ``ledger_id`` and ``source``, the only
envelope field read is ``ledger_seq``: it is assigned by the judge and cannot
be derived from ``raw_row``. Other payload fields and hook lane attribution are
ignored. An emitter that cannot afford the parser still publishes a correct event.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelWorkLedgerFoldRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    raw_row: str = Field(..., min_length=1)
    ledger_id: str = Field(default="rolling-work-ledger", min_length=1)
    source: str = Field(default="unknown", min_length=1)
    ledger_seq: int | None = Field(default=None, ge=1)


__all__: list[str] = ["ModelWorkLedgerFoldRequest"]
