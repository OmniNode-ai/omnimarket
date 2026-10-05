# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The orchestrator's command to its ledger effect (OMN-20636)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ModelPrHandoffLedgerAppendCommand(BaseModel):
    """Append these exact rows through the ledger's bus append and answer for ``handoff_key``.

    ``ledger_request_id`` is the same on every attempt of one handoff, so a
    resend after a lost receipt is deduplicated by the ledger host and never
    appends twice. ``attempt`` counts from 1.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    handoff_key: str = Field(..., min_length=3)
    ledger_request_id: UUID
    attempt: int = Field(..., ge=1)
    rows: str = Field(..., min_length=1, max_length=65536)
    requested_by_lane: str
    requesting_host: str
    requested_at: Annotated[datetime, AwareDatetime]


__all__: list[str] = ["ModelPrHandoffLedgerAppendCommand"]
