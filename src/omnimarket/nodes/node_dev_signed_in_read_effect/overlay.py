# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev signed-in read overlay (OMN-24345): every deployment fact the node needs, found by reference.

No defaults for deployment facts: a lane, a realm, a host, a route or a key the overlay leaves out is refused, not filled.
The user password is read from the secret store by folder and key name only; no value is held here.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.secret_store.model_secret_store_overlay import (
    ModelSecretStoreOverlay,
)

NODE_NAME = "node_dev_signed_in_read_effect"


class ModelKeycloakOverlay(BaseModel):
    """The Keycloak token endpoint the node signs in at; the realm is checked against the contract's dev-only block."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: str
    realm: str
    timeout_seconds: float = Field(gt=0)
    token_path_template: str


class ModelUserOverlay(BaseModel):
    """The dev user the node signs in as and where its password is stored."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    username: str
    client_id: str
    password_folder: str
    password_key: str


class ModelApiOverlay(BaseModel):
    """The onex-api the read goes to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: str
    timeout_seconds: float = Field(gt=0)


class ModelReadOverlay(BaseModel):
    """The one read: its route, its query names and values, and the data states a healthy answer may carry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    correlation_param: str
    correlation_id: str
    limit_param: str
    limit: int = Field(gt=0)
    data_state_field: str
    data_states: tuple[str, ...] = Field(min_length=1)


class ModelDevSignedInReadOverlay(BaseModel):
    """The whole overlay document; the optional store block means the machine's own store when absent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    keycloak: ModelKeycloakOverlay
    user: ModelUserOverlay
    api: ModelApiOverlay
    read: ModelReadOverlay
    secret_store: ModelSecretStoreOverlay | None = None


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelDevSignedInReadOverlay:
    """The overlay document at ``<root>/node_dev_signed_in_read_effect/overlay.yaml``, validated."""
    return load_structured_overlay(NODE_NAME, ModelDevSignedInReadOverlay, environ)


__all__ = [
    "NODE_NAME",
    "ModelApiOverlay",
    "ModelDevSignedInReadOverlay",
    "ModelKeycloakOverlay",
    "ModelReadOverlay",
    "ModelUserOverlay",
    "load_overlay",
]
