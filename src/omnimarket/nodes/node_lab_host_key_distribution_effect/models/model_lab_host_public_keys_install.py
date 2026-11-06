# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, declared key set and result for installing declared public keys on pool hosts."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.models.lab_host_key import public_key_parts
from omnimarket.models.overlay_keys import ModelLabHostsTable

PUBLIC_KEYS_OVERLAY_ID = "lab_host_public_keys"


class ModelLabHostPublicKeysOverlay(BaseModel):
    """The document ``<root>/lab_host_public_keys/overlay.yaml`` holds: the declared public key lines."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    keys: tuple[str, ...] = Field(min_length=1)

    @field_validator("keys")
    @classmethod
    def validate_keys(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            public_key_parts(value)
        if len({public_key_parts(value) for value in values}) != len(values):
            raise ValueError("declared public keys repeat")
        return values


class EnumHostInstallStatus(StrEnum):
    """The outcome of one pool host."""

    OK = "ok"
    PLANNED = "planned"
    REFUSED = "refused"
    SKIPPED = "skipped"


class ModelLabHostPublicKeysInstallRequest(BaseModel):
    """Install the declared key set on every available pool host, or on the named hosts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hosts: tuple[str, ...] = ()
    table: ModelLabHostsTable | None = None
    table_path: str | None = None
    keys: ModelLabHostPublicKeysOverlay | None = None
    dry_run: bool = False
    local_host: str | None = None
    timeout_s: int = Field(default=30, ge=1)


class ModelHostInstallResult(BaseModel):
    """What one pool host took. Counts only: no key text and no address."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    status: EnumHostInstallStatus
    keys_added: int = 0
    keys_present: int = 0
    detail: str = ""


class ModelLabHostPublicKeysInstallResult(BaseModel):
    """Hosts in table order, the process exit code and any scope or overlay refusal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dry_run: bool
    hosts: tuple[ModelHostInstallResult, ...] = ()
    exit_code: int
    refusal: str | None = None

    def lines(self) -> tuple[str, ...]:
        verb = "would-add" if self.dry_run else "added"
        return tuple(
            f"{row.host} {row.status.value} {verb}={row.keys_added} "
            f"present={row.keys_present}" + (f" {row.detail}" if row.detail else "")
            for row in self.hosts
        )
