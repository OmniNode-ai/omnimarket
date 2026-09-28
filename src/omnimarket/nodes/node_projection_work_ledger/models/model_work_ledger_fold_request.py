# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Fold request: one row event as it arrives off the bus (OMN-19513).

``raw_row`` is the truth. Every other payload field (the typed ones the emit
node adds, and the envelope metadata and hook lane attribution the emit paths
inject) is ignored, and the typed view is re-derived from ``raw_row`` by the
same pure parser the emit node uses. An emitter that cannot afford the parser
therefore publishes a correct event.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelWorkLedgerFoldRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    raw_row: str = Field(..., min_length=1)
    ledger_id: str = Field(default="rolling-work-ledger", min_length=1)
    source: str = Field(default="unknown", min_length=1)


__all__: list[str] = ["ModelWorkLedgerFoldRequest"]
