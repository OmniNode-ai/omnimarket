# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed rows for minting a broker service principal login and delivering its credential file.

The overlay holds the deployment facts of each declared principal: its broker addressing, the
secret store folder and key for its password, and the read-only folders on each host that
receive the credential env file. Rows carry names, folders, keys and modes only, never a
credential value.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.secret_store.model_secret_store_overlay import (
    ModelSecretStoreOverlay,
)


class ModelServicePrincipalBroker(BaseModel):
    """One broker's addressing facts. Every field is a name or identifier, never a credential."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    host: str = Field(min_length=1)
    compose_project: str = Field(min_length=1)
    broker_container: str = Field(min_length=1)
    superuser_source_container: str = Field(min_length=1)
    ssh_target: str = Field(min_length=1)
    mechanism: str = Field(min_length=1)


class ModelServicePrincipalDelivery(BaseModel):
    """One host's credential file delivery target."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    ssh_target: str = Field(min_length=1)
    folder: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    mode: str = Field(pattern=r"^[0-7]{3,4}$")
    privilege_argv: tuple[str, ...] = ()


class ModelServicePrincipalDeclaration(BaseModel):
    """One declared service principal: its broker login name, secret store location, and delivery targets."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    vault_folder: str = Field(min_length=1)
    password_key: str = Field(min_length=1)
    password_bytes: int = Field(ge=16)
    username_env: str = Field(min_length=1)
    password_env: str = Field(min_length=1)
    broker: ModelServicePrincipalBroker
    deliveries: tuple[ModelServicePrincipalDelivery, ...] = Field(min_length=1)


class ModelServicePrincipalMintOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    principals: tuple[ModelServicePrincipalDeclaration, ...] = Field(min_length=1)
    # The secret store the passwords live in; with none, the machine's own store configuration is used.
    secret_store: ModelSecretStoreOverlay | None = None

    @model_validator(mode="after")
    def _no_duplicate_names(self) -> ModelServicePrincipalMintOverlay:
        names = [p.name for p in self.principals]
        if len(names) != len(set(names)):
            raise ValueError("duplicate principal names in overlay")
        return self


class ModelServicePrincipalMintRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    principal: str = Field(min_length=1)
    check_only: bool = False
    correlation_id: str = ""


class ModelServicePrincipalDeliveryReading(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    state: Literal["unchanged", "written", "would-write", "failed"]
    owner: str
    mode: str
    detail: str = ""


class ModelServicePrincipalMintResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    principal: str
    outcome: Literal["created", "exists", "would-create", "refused", "failed"]
    refusal_kind: Literal[
        "",
        "undeclared",
        "login_without_secret",
        "broker_unreachable",
        "vault_unavailable",
        "delivery_failed",
    ] = ""
    broker_login: Literal["created", "existed", "would-create", "absent", "failed"]
    secret: Literal["created", "existed", "would-create", "absent", "failed"]
    deliveries: tuple[ModelServicePrincipalDeliveryReading, ...]
    detail: str = ""
    correlation_id: str = ""

    @property
    def ok(self) -> bool:
        """True when the outcome is a success state."""
        return self.outcome in ("created", "exists", "would-create")
