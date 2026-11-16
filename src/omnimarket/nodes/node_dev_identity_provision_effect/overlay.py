# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev identity provision overlay (OMN-24115): every deployment fact the node needs, found by reference.

No defaults for deployment facts: a lane, a realm, a host or a key the overlay leaves out is refused, not filled. The
admin credential and the user password are read from the secret store by folder and key name only; no value is held here.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.secret_store.model_secret_store_overlay import (
    ModelSecretStoreOverlay,
)

NODE_NAME = "node_dev_identity_provision_effect"


class ModelKeycloakOverlay(BaseModel):
    """The Keycloak endpoints the node talks to; the realm is checked against the contract's dev-only block."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: str
    realm: str
    timeout_seconds: float = Field(gt=0)
    token_path_template: str
    admin_users_path_template: str
    admin_reset_password_path_template: str


class ModelAdminOverlay(BaseModel):
    """The Keycloak admin credential, read from the secret store by reference only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    client_id: str
    secret_folder: str
    secret_key: str


class ModelUserOverlay(BaseModel):
    """The dev user the node creates; the password key must equal a declared generated key (a seam test checks it)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    username: str
    email: str
    first_name: str
    last_name: str
    client_id: str
    password_key_id: str
    password_folder: str
    password_key: str


class ModelApiOverlay(BaseModel):
    """The onex-api the node provisions a probe tenant against."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: str
    provision_path: str
    timeout_seconds: float = Field(gt=0)


class ModelDevIdentityProvisionOverlay(BaseModel):
    """The whole overlay document; the optional store block means the machine's own store when absent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    keycloak: ModelKeycloakOverlay
    admin: ModelAdminOverlay
    user: ModelUserOverlay
    api: ModelApiOverlay
    secret_store: ModelSecretStoreOverlay | None = None


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelDevIdentityProvisionOverlay:
    """The overlay document at ``<root>/node_dev_identity_provision_effect/overlay.yaml``, validated."""
    return load_structured_overlay(NODE_NAME, ModelDevIdentityProvisionOverlay, environ)


__all__ = [
    "NODE_NAME",
    "ModelAdminOverlay",
    "ModelApiOverlay",
    "ModelDevIdentityProvisionOverlay",
    "ModelKeycloakOverlay",
    "ModelUserOverlay",
    "load_overlay",
]
