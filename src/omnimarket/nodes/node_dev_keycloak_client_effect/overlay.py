# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev keycloak client overlay (OMN-24237): every deployment fact the node needs, found by reference.

No defaults for deployment facts: a lane, a realm, a host or a key the overlay leaves out is refused, not filled. The
bootstrap admin credential is read from the host env file by name only and the client secret from the secret store by
folder and key name only; no value is held here.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.secret_store.model_secret_store_overlay import (
    ModelSecretStoreOverlay,
)

NODE_NAME = "node_dev_keycloak_client_effect"


class ModelKeycloakOverlay(BaseModel):
    """The Keycloak endpoints the node talks to; the realm is checked against the contract's dev-only block."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: str
    realm: str
    admin_realm: str
    timeout_seconds: float = Field(gt=0)
    token_path_template: str
    clients_path_template: str
    client_path_template: str
    service_account_user_path_template: str
    client_role_path_template: str
    user_client_role_mappings_path_template: str
    client_scope_mappings_path_template: str
    client_protocol_mappers_path_template: str
    client_protocol_mapper_path_template: str


class ModelBootstrapOverlay(BaseModel):
    """The Keycloak bootstrap admin credential, read from the host env file by name only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    env_file: Path
    username_name: str
    password_name: str
    admin_client_id: str


class ModelClientOverlay(BaseModel):
    """The one confidential service-account client the node ensures; its secret lives in the store, never here."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    client_id: str
    management_client_id: str
    roles: list[str]
    secret_key_id: str
    secret_folder: str
    secret_key: str


class ModelProtocolMapperOverlay(BaseModel):
    """One OIDC protocol mapper the node ensures on the sign-in client; its config is deployment fact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    protocol_mapper: str
    config: dict[str, str]


class ModelSignInClientOverlay(BaseModel):
    """The one sign-in client whose tenant claim mappers the node ensures, by declared client id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    client_id: str
    mappers: list[ModelProtocolMapperOverlay] = Field(min_length=1)


class ModelDevKeycloakClientOverlay(BaseModel):
    """The whole overlay document; the optional store block means the machine's own store when absent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    keycloak: ModelKeycloakOverlay
    bootstrap: ModelBootstrapOverlay
    client: ModelClientOverlay
    sign_in_client: ModelSignInClientOverlay
    secret_store: ModelSecretStoreOverlay | None = None


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelDevKeycloakClientOverlay:
    """The overlay document at ``<root>/node_dev_keycloak_client_effect/overlay.yaml``, validated."""
    return load_structured_overlay(NODE_NAME, ModelDevKeycloakClientOverlay, environ)


__all__ = [
    "NODE_NAME",
    "ModelBootstrapOverlay",
    "ModelClientOverlay",
    "ModelDevKeycloakClientOverlay",
    "ModelKeycloakOverlay",
    "ModelProtocolMapperOverlay",
    "ModelSignInClientOverlay",
    "load_overlay",
]
