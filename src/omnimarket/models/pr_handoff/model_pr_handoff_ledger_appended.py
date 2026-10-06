# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ledger effect's answer for one append attempt (OMN-20636)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.models.pr_handoff.enum_pr_handoff_ledger_status import (
    EnumPrHandoffLedgerStatus,
)


class ModelPrHandoffLedgerAppended(BaseModel):
    """Exactly one per :class:`ModelPrHandoffLedgerAppendCommand`, whatever happened."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    handoff_key: str = Field(..., min_length=3)
    ledger_request_id: UUID
    attempt: int = Field(..., ge=1)
    status: EnumPrHandoffLedgerStatus
    ledger_lines: tuple[int, ...] = ()
    ledger_host: str = ""
    message: str = Field(default="", max_length=2000)
    answered_at: Annotated[datetime, AwareDatetime]


__all__: list[str] = ["ModelPrHandoffLedgerAppended"]
