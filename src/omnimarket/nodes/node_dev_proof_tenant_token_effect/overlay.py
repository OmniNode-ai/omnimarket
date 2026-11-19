# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev proof tenant token overlay: every deployment fact the node needs, found by reference.

No defaults for deployment facts: a lane, a store address, a key name or a secret name the overlay leaves out is refused,
not filled. The lab test key and the two tokens are named here and live in the dev secret store; no value is held here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.secret_store.model_secret_store_overlay import (
    ModelSecretStoreOverlay,
)

NODE_NAME = "node_dev_proof_tenant_token_effect"


class ModelSigningKeyOverlay(BaseModel):
    """Where the lab test private key sits in the dev secret store, by folder and name."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    secret_folder: str = Field(pattern=r"^/")
    secret_key: str = Field(min_length=1)
    algorithm: Literal["RS256", "ES256"]


class ModelProofTenantOverlay(BaseModel):
    """One proof tenant: its slug and id as the door's binding rows hold them, and the secret name of its token."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_slug: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    secret_name: str = Field(min_length=1)


class ModelDevProofTenantTokenOverlay(BaseModel):
    """The whole overlay document. The store block must address the dev store the seed node writes to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    store: ModelSecretStoreOverlay
    signing_key: ModelSigningKeyOverlay
    token_folder: str = Field(pattern=r"^/")
    issuer: str | None = Field(default=None, min_length=1)
    tenants: list[ModelProofTenantOverlay] = Field(min_length=1)

    @model_validator(mode="after")
    def _store_addressed_and_names_distinct(self) -> Self:
        missing = [
            name
            for name in ("address", "project_id", "environment")
            if getattr(self.store, name) is None
        ]
        if missing:
            raise ValueError(f"store block lacks {missing}: the seed node needs each")
        for field in ("tenant_slug", "tenant_id", "secret_name"):
            values = [getattr(tenant, field) for tenant in self.tenants]
            if len(set(values)) != len(values):
                raise ValueError(f"tenants repeat a {field}")
        return self


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelDevProofTenantTokenOverlay:
    """The overlay document at ``<root>/node_dev_proof_tenant_token_effect/overlay.yaml``, validated."""
    return load_structured_overlay(NODE_NAME, ModelDevProofTenantTokenOverlay, environ)


__all__ = [
    "NODE_NAME",
    "ModelDevProofTenantTokenOverlay",
    "ModelProofTenantOverlay",
    "ModelSigningKeyOverlay",
    "load_overlay",
]
