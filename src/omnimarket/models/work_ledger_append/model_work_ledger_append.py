# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ledger append command and receipt (OMN-20275)."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
)

PrincipalName = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]
# One pipe-free token: the value of a row's id cell, matched as a whole pipe cell.
RowId = Annotated[
    str,
    Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$"),
]


class EnumWorkLedgerAppendStatus(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REFUSED = "refused"
    ERROR = "error"
    # append_if_absent found a row carrying the requested row id and appended nothing.
    ALREADY_PRESENT = "already-present"
    # OMN-23154: the serve is in a fenced handover and took nothing; the caller sends the command again.
    RETRY = "retry"


class ModelWorkLedgerAppendRequest(BaseModel):
    """Exact rows and caller attribution; the writer comes from the broker, never the payload."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    ledger_id: str = "rolling-work-ledger"
    rows: str = Field(min_length=1, max_length=65536)
    requested_by_lane: str
    requesting_host: str
    requested_at: Annotated[datetime, AwareDatetime]
    # append_if_absent (OMN-22185): when set, the one row of this request is appended
    # only if no ledger row already carries the contract-declared id cell with this
    # value; otherwise nothing is appended and the receipt is already-present with
    # the existing row's line. None is the plain append operation.
    append_if_absent_row_id: RowId | None = None

    @model_serializer(mode="wrap")
    def _omit_unset_optionals(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        """Put an optional field on the wire only when it carries a value.

        The ledger host validates this model with ``extra="forbid"``, so a host older than a field refuses
        a request that merely names it, even as ``null`` (OMN-19998: a writer at the append_if_absent
        commit was refused by a host one commit behind, "Extra inputs are not permitted", and every lane
        CLAIM and TERMINAL from that writer was lost for 17 minutes). A request that does not use an
        additive optional field is therefore byte-for-byte the request an older host reads. A request
        that sets one is still refused by an older host, loudly, because dropping a set
        ``append_if_absent_row_id`` would turn a conditional append into an unconditional one.
        """
        data: dict[str, Any] = handler(self)
        optional_none = [
            name
            for name, field in type(self).model_fields.items()
            if not field.is_required() and data.get(name) is None and name in data
        ]
        for name in optional_none:
            del data[name]
        return data


class ModelAppendIfAbsentPolicy(BaseModel):
    """The ``append_if_absent`` section of the node contract: names, suffix and wait, never code constants."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id_cell: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    lock_suffix: str = Field(pattern=r"^\.[A-Za-z0-9._-]+$")
    lock_wait_seconds: float = Field(gt=0)


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
    # OMN-23154: after the flip the row lands on the bus, not in the file; this is the sequence of the
    # request's first row (its other rows take the next numbers) and ledger_lines stays empty.
    ledger_seq: int | None = Field(default=None, ge=1)

    @model_serializer(mode="wrap")
    def _omit_unset_ledger_seq(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        """A receipt from the file path is byte-for-byte what a reader older than ledger_seq accepts."""
        data: dict[str, Any] = handler(self)
        if data.get("ledger_seq") is None:
            data.pop("ledger_seq", None)
        return data


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
