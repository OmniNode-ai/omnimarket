# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the iLO setup verb (OMN-22870).

The setup verb does, for one or more hosts, what the skill listed as five hand steps: two
least-privilege accounts, their secret-store keys, the pinned certificate, the overlay row and the
first read with the serial recorded. Nothing here holds a credential: the result names steps, key
names and refusal codes only.
"""

from __future__ import annotations

from enum import StrEnum, unique
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

HostName = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
Address = Annotated[
    str, Field(min_length=1, max_length=253, pattern=r"^[A-Za-z0-9][A-Za-z0-9.:-]*$")
]


@unique
class EnumIloSetupRefusal(StrEnum):
    """Why one host's setup stopped. Codes only, never text from the device."""

    ADDRESS_MISSING = "address_missing"
    ADMIN_CREDENTIAL_MISSING = "admin_credential_missing"
    SECRET_STORE_UNAVAILABLE = "secret_store_unavailable"
    STORE_INCONSISTENT = "store_inconsistent"
    TLS_FAILED = "tls_failed"
    CONNECT_FAILED = "connect_failed"
    AUTH_FAILED = "auth_failed"
    INSUFFICIENT_PRIVILEGE = "insufficient_privilege"
    ENDPOINT_MISSING = "endpoint_missing"
    FINGERPRINT_MISMATCH = "fingerprint_mismatch"
    FINGERPRINT_UNCONFIRMED = "fingerprint_unconfirmed"
    GENERATION_UNRECOGNIZED = "generation_unrecognized"
    ACCOUNT_EXISTS_NO_CREDENTIAL = "account_exists_no_credential"
    ACCOUNT_TOO_PRIVILEGED = "account_too_privileged"
    ACCOUNT_PRIVILEGE_SHORT = "account_privilege_short"
    ACCOUNT_PRIVILEGES_UNREADABLE = "account_privileges_unreadable"
    ACCOUNT_CREATE_FAILED = "account_create_failed"
    ACCOUNT_LOGIN_FAILED = "account_login_failed"
    READ_FAILED = "read_failed"
    IDENTITY_MISMATCH = "identity_mismatch"
    OVERLAY_INVALID = "overlay_invalid"
    OVERLAY_WRITE_FAILED = "overlay_write_failed"
    LAB_CA_UNAVAILABLE = "lab_ca_unavailable"
    CSR_UNSUPPORTED = "csr_unsupported"
    CSR_FAILED = "csr_failed"
    CERTIFICATE_IMPORT_FAILED = "certificate_import_failed"
    ILO_RESET_TIMEOUT = "ilo_reset_timeout"
    ISSUER_UNKNOWN = "issuer_unknown"


class ModelIloSetupRequest(BaseModel):
    """The hosts to set up and what the operator supplied beside them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hosts: Annotated[tuple[HostName, ...], Field(min_length=1)]
    # host name -> iLO address, for a host the lab host table and the overlay do not give one.
    addresses: dict[HostName, Address] = Field(default_factory=dict)
    # The iLO administrator account the stored or prompted password belongs to.
    admin_user: Annotated[str, Field(min_length=1, max_length=39, pattern=r"^\S+$")] = (
        "Administrator"
    )


class ModelIloSetupStep(BaseModel):
    """One thing the setup did, or found already done."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    step: str
    state: Literal["done", "reused", "refused"]
    note: str = ""


class ModelIloSetupHostResult(BaseModel):
    """How one host ended."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    status: Literal["ready", "refused"]
    refusal: EnumIloSetupRefusal | None = None
    detail: str | None = None
    steps: tuple[ModelIloSetupStep, ...] = ()
    # False when the standalone node reader cannot see the stored accounts yet.
    node_reader_ready: bool | None = None


class ModelIloSetupResult(BaseModel):
    """All hosts of one run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hosts: tuple[ModelIloSetupHostResult, ...]

    @property
    def all_ready(self) -> bool:
        return all(h.status == "ready" for h in self.hosts)
