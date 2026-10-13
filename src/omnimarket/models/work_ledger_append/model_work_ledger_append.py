# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ledger append command and receipt (OMN-20275)."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

PrincipalName = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]


class EnumWorkLedgerAppendStatus(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REFUSED = "refused"
    ERROR = "error"


class ModelWorkLedgerAppendRequest(BaseModel):
    """Exact rows and caller attribution; the writer comes from the broker, never the payload."""

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
    principal: PrincipalName | None = None


class ModelWorkLedgerTerminalRefused(BaseModel):
    """The ledger host refused a request carrying TERMINAL rows.

    The CLAIMs those rows would have closed stay open on the ledger. Owner
    claims are leases (a claim owns only until its contract-declared TTL), so
    the refused TERMINAL holds no PR past that TTL; this event makes the
    refusal observable instead of silent.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event: Literal["TERMINAL_REFUSED"] = "TERMINAL_REFUSED"
    request_id: UUID
    requested_by_lane: str
    requesting_host: str
    ledger_host: str
    principal: PrincipalName | None = None
    exit_code: int
    reason: str = Field(max_length=2000)
    terminal_lanes: tuple[str, ...]
    tickets: tuple[str, ...]
    prs: tuple[str, ...]
    refused_at: Annotated[datetime, AwareDatetime]
