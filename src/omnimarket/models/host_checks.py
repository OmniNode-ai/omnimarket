# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed facts and durable tick receipts for the two host state checks.

Identity and command bindings are deployment data. No credential value is a
field of a fact, finding, cursor or exception.
"""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, PositiveInt

from omnimarket.models.liveness.model_automation_liveness import (
    ModelAutomationLivenessEntry,
)


class ModelHostCheckData(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelAccountIdentity(ModelHostCheckData):
    label: str
    org_hash: str
    email_hash: str


class ModelHostReadBinding(ModelHostCheckData):
    host: str
    auth_read: tuple[str, ...]
    revision_read: tuple[str, ...]
    contains_read: tuple[str, ...]
    principals_read: tuple[str, ...]


class ModelHostCheckPolicy(ModelHostCheckData):
    hosts: tuple[ModelHostReadBinding, ...] = Field(min_length=1)
    account_identities: tuple[ModelAccountIdentity, ...] = ()
    pool_accounts: tuple[str, ...] = Field(min_length=1)
    install_principals: tuple[str, ...] = Field(min_length=1)
    check_principal: str = Field(min_length=1)
    token_switch_fix: str = Field(min_length=1)
    install_claim_fix: str = Field(min_length=1)
    ledger_path: str
    exception_state_dir: str
    read_timeout_seconds: PositiveInt
    interval_seconds: PositiveInt
    liveness_entries: dict[str, ModelAutomationLivenessEntry]


class ModelHostFact(ModelHostCheckData):
    host: str
    account_label: str | None = None
    installed_revision: str | None = None
    contains: dict[str, bool | None] = Field(default_factory=dict)


class ModelInstallClaim(ModelHostCheckData):
    row_id: str
    host: str
    sha: str
    writer_principal: str | None = None
    actor: str = ""
    row_type: Literal["STATUS", "TERMINAL"] = "STATUS"


class ModelHostAccountRequest(ModelHostCheckData):
    host: ModelHostFact
    pool_accounts: tuple[str, ...]
    token_switch_fix: str


class ModelInstallClaimRequest(ModelHostCheckData):
    claim: ModelInstallClaim
    host: ModelHostFact | None = None
    install_principals: tuple[str, ...]
    fix: str = ""


class ModelHostCheckFinding(ModelHostCheckData):
    check: Literal["S7", "S9"]
    host: str
    verdict: Literal["OK", "FINDING", "UNREAD"]
    reason: str
    key: str
    account_label: str | None = None
    claim_id: str | None = None
    fix: str = ""
    accepted: bool = False


class ModelHostCheckTick(ModelHostCheckData):
    tick_id: str = Field(min_length=1)
    at: AwareDatetime


class ModelHostCheckCursor(ModelHostCheckData):
    check: Literal["S7", "S9"]
    tick_id: str
    at: AwareDatetime
    verdict: Literal["OK", "FINDING", "UNREAD"]
    rows_read: int


class ModelHostCheckSnapshot(ModelHostCheckData):
    hosts: tuple[ModelHostFact, ...] = ()
    claims: tuple[ModelInstallClaim, ...] = ()
    ledger_readable: bool = False
    prerequisite_error: str = ""


class ModelHostCheckTickResult(ModelHostCheckData):
    tick_id: str
    findings: tuple[ModelHostCheckFinding, ...]
    cursors: tuple[ModelHostCheckCursor, ...]
    exceptions_added: int = 0
    pending_events: int = 0
    error: str = ""


class ModelHostCheckEvent(ModelHostCheckData):
    """A durable outbox entry, published under its stable idempotency key."""

    key: str
    topic: str
    payload: dict[str, object]
