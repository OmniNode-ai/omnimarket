# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Requests, public material and results for lab host key distribution."""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.models.lab_host_key import public_key_parts
from omnimarket.models.overlay_keys import ModelLabHostsTable


class EnumPairStatus(StrEnum):
    """The outcome of one ordered host pair."""

    OK = "ok"
    PLANNED = "planned"
    REFUSED = "refused"


class ModelLabHostKeyDistributionRequest(BaseModel):
    """Distribute public keys between the explicitly selected hosts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hosts: tuple[str, ...] = Field(min_length=1)
    table: ModelLabHostsTable | None = None
    table_path: str | None = None
    dry_run: bool = False
    local_host: str | None = None
    # `available: false` in the host table is a placement flag, so a host that takes no lanes can
    # still need its keys: the operator names each such host here and it is reached by key
    # distribution alone. Every name must also be in `hosts`; no other host is admitted.
    allow_unavailable: tuple[str, ...] = ()
    identity_public_files: tuple[str, ...] = (
        "id_ed25519.pub",
        "id_ecdsa.pub",
        "id_rsa.pub",
    )
    host_key_files: tuple[str, ...] = ("/etc/ssh/ssh_host_ed25519_key.pub",)
    timeout_s: int = Field(default=30, ge=1)

    @model_validator(mode="after")
    def validate_allow_unavailable_in_scope(
        self,
    ) -> ModelLabHostKeyDistributionRequest:
        outside = [name for name in self.allow_unavailable if name not in self.hosts]
        if outside:
            raise ValueError(
                f"allow_unavailable names hosts outside the scope: {', '.join(outside)}"
            )
        return self

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
