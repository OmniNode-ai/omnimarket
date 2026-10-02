# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ledger append command and receipt (OMN-20275)."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class EnumWorkLedgerAppendStatus(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REFUSED = "refused"
    ERROR = "error"


class ModelWorkLedgerAppendRequest(BaseModel):
    """Complete rows built on the requesting host, sent without rewriting."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    ledger_id: str = "rolling-work-ledger"
    rows: str = Field(min_length=1, max_length=65536)
    requested_by_lane: str
    requesting_host: str
    requested_at: Annotated[datetime, AwareDatetime]


class ModelWorkLedgerAppendReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    status: EnumWorkLedgerAppendStatus
    exit_code: int
    message: str = Field(max_length=2000)
    ledger_lines: list[Annotated[int, Field(ge=1)]]
    ledger_host: str
    duration_ms: int = Field(ge=0)
