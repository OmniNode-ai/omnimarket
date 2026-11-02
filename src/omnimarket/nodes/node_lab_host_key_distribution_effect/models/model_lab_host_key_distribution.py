# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Requests, public material and results for lab host key distribution."""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.models.overlay_keys import ModelLabHostsTable


class EnumPairStatus(StrEnum):
    """The outcome of one ordered host pair."""

    OK = "ok"
    PLANNED = "planned"
    REFUSED = "refused"


class EnumKeyFile(StrEnum):
    """The only files the transport may append to."""

    AUTHORIZED_KEYS = "authorized_keys"
    KNOWN_HOSTS = "known_hosts"


class EnumLineState(StrEnum):
    """Whether a requested line exists, was appended or would be appended."""

    PRESENT = "present"
    ADDED = "added"
    ABSENT = "absent"


class ModelLabHostKeyDistributionRequest(BaseModel):
    """Distribute public keys between the explicitly selected hosts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hosts: tuple[str, ...] = Field(min_length=1)
    table: ModelLabHostsTable | None = None
    table_path: str | None = None
    dry_run: bool = False
    local_host: str | None = None
    identity_public_files: tuple[str, ...] = (
        "id_ed25519.pub",
        "id_ecdsa.pub",
        "id_rsa.pub",
    )
    host_key_files: tuple[str, ...] = ("/etc/ssh/ssh_host_ed25519_key.pub",)
    timeout_s: int = Field(default=30, ge=1)

    @field_validator("identity_public_files")
    @classmethod
    def validate_identity_files(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values or any(
            not re.fullmatch(r"[A-Za-z0-9_.-]+\.pub", value) or ".." in value
            for value in values
        ):
            raise ValueError(
                "identity files must be public .pub basenames without '..'"
            )
        return values

    @field_validator("host_key_files")
    @classmethod
    def validate_host_files(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values or any(
            not re.fullmatch(r"/[A-Za-z0-9_./-]+\.pub", value)
            or ".." in value.split("/")
            for value in values
        ):
            raise ValueError("host key files must be absolute public .pub paths")
        return values


def public_key_parts(line: str) -> str:
    """Return the key type and encoded public key, excluding any comment."""
    parts = line.split()
    if (
        "\n" in line
        or "\r" in line
        or len(parts) < 2
        or not re.fullmatch(r"(?:ssh-|ecdsa-|sk-)[A-Za-z0-9@._+-]+", parts[0])
        or not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", parts[1])
    ):
        raise ValueError("invalid public key line")
    return " ".join(parts[:2])


class ModelHostKeyMaterial(BaseModel):
    """Only the host's public user key and public host keys."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_key: str
    host_keys: tuple[str, ...] = Field(min_length=1)

    @field_validator("user_key")
    @classmethod
    def validate_user_key(cls, value: str) -> str:
        public_key_parts(value)
        return value

    @field_validator("host_keys")
    @classmethod
    def validate_host_keys(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            public_key_parts(value)
        return values


class ModelPairResult(BaseModel):
    """The two append outcomes for one source and destination."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str
    destination: str
    status: EnumPairStatus
    authorized_key_added: bool
    known_hosts_added: bool
    detail: str = ""


class ModelLabHostKeyDistributionResult(BaseModel):
    """Deterministically ordered pair outcomes and the process exit code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dry_run: bool
    pairs: tuple[ModelPairResult, ...] = ()
    exit_code: int
    refusal: str | None = None

    def lines(self) -> tuple[str, ...]:
        def state(added: bool) -> str:
            return ("would-add" if self.dry_run else "added") if added else "present"

        return tuple(
            f"{pair.source}->{pair.destination} {pair.status.value} "
            f"authorized_keys={state(pair.authorized_key_added)} "
            f"known_hosts={state(pair.known_hosts_added)}"
            + (f" {pair.detail}" if pair.status == EnumPairStatus.REFUSED else "")
            for pair in self.pairs
        )


class EnumInstallStatus(StrEnum):
    """The outcome of one host in a declared-key install."""

    OK = "ok"
    PLANNED = "planned"
    REFUSED = "refused"
    SKIPPED = "skipped"


class ModelDeclaredKeysInstallRequest(BaseModel):
    """Install the overlay-declared public keys on every pool host of the table."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    table: ModelLabHostsTable | None = None
    table_path: str | None = None
    # Explicit public keys; omitted, the node's overlay supplies them.
    public_keys: tuple[str, ...] | None = None
    # Explicit host names; omitted, every available non-contributor host of the table.
    hosts: tuple[str, ...] | None = None
    dry_run: bool = False
    local_host: str | None = None
    timeout_s: int = Field(default=30, ge=1)

    @field_validator("public_keys")
    @classmethod
    def validate_keys(cls, values: tuple[str, ...] | None) -> tuple[str, ...] | None:
        for value in values or ():
            public_key_parts(value)
        return values


class ModelHostInstallReceipt(BaseModel):
    """The receipt row of one host: how many declared keys were added or already there."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    status: EnumInstallStatus
    keys_added: int = 0
    keys_present: int = 0
    detail: str = ""


class ModelDeclaredKeysInstallResult(BaseModel):
    """One receipt row per host in table order, and the process exit code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dry_run: bool
    receipts: tuple[ModelHostInstallReceipt, ...] = ()
    exit_code: int
    refusal: str | None = None

    def lines(self) -> tuple[str, ...]:
        verb = "would-add" if self.dry_run else "added"
        return tuple(
            f"{r.host} {r.status.value} {verb}={r.keys_added} present={r.keys_present}"
            + (f" {r.detail}" if r.detail else "")
            for r in self.receipts
        )
