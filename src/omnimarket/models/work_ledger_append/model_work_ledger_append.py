# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ledger append command and receipt (OMN-20275)."""

import base64
import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

PrincipalName = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")]


class ModelWorkLedgerPrincipalRecords(BaseModel):
    """Issuer-owned public records, supplied locally to the ledger host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    public_keys: dict[PrincipalName, str] = Field(min_length=1)

    @field_validator("public_keys")
    @classmethod
    def validate_keys(cls, keys: dict[str, str]) -> dict[str, str]:
        for value in keys.values():
            Ed25519PublicKey.from_public_bytes(base64.b64decode(value, validate=True))
        return keys

    def verification_keys(self) -> dict[str, Ed25519PublicKey]:
        return {
            principal: Ed25519PublicKey.from_public_bytes(
                base64.b64decode(value, validate=True)
            )
            for principal, value in self.public_keys.items()
        }


class EnumWorkLedgerAppendStatus(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REFUSED = "refused"
    ERROR = "error"


class ModelWorkLedgerAppendRequest(BaseModel):
    """Exact rows and signed attribution; unsigned legacy requests fail closed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    ledger_id: str = "rolling-work-ledger"
    rows: str = Field(min_length=1, max_length=65536)
    requested_by_lane: str
    requesting_host: str
    requested_at: Annotated[datetime, AwareDatetime]
    principal: PrincipalName | None = None
    signature: str | None = Field(default=None, min_length=1, max_length=512)

    def signing_bytes(self) -> bytes:
        """Domain-separated canonical JSON covers every field except signature."""
        payload = self.model_dump(mode="json", exclude={"signature"})
        return b"onex.work-ledger-append.v1\0" + json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")

    def signed(
        self, principal: str, key: Ed25519PrivateKey
    ) -> "ModelWorkLedgerAppendRequest":
        request = type(self).model_validate(
            self.model_dump() | {"principal": principal, "signature": None}
        )
        signature = base64.b64encode(key.sign(request.signing_bytes())).decode("ascii")
        return request.model_copy(update={"signature": signature})


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
