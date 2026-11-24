# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stability gateway key provision overlay (OMN-26586): every deployment fact the node needs, found by reference.

Found at ``<root>/node_stability_gateway_key_provision_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. Nothing here has a default except the optional store block (absent means the machine's own
store). The identity database's address is never in the overlay: ``database.dsn_secret`` is the logical name the secret
store resolves, and no value is held here. The lane is checked against the contract's ``stability_only`` block.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.secret_store.model_secret_store_overlay import (
    ModelSecretStoreOverlay,
)
from omnimarket.nodes.node_tenant_provision_effect.handlers.tenant_identity_postgres import (
    TenantIdentityPostgres,
)
from omnimarket.nodes.node_tenant_provision_effect.overlay import (
    ModelTenantIdentityDatabaseOverlay,
    ModelTenantProvisionOverlay,
    build_store,
)

NODE_NAME = "node_stability_gateway_key_provision_effect"


class ModelStabilityGatewayKeyProvisionOverlay(BaseModel):
    """The whole overlay document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    database: ModelTenantIdentityDatabaseOverlay
    secret_store: ModelSecretStoreOverlay | None = None


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelStabilityGatewayKeyProvisionOverlay:
    """The overlay document, validated."""
    return load_structured_overlay(
        NODE_NAME, ModelStabilityGatewayKeyProvisionOverlay, environ
    )


def build_identity_store(
    overlay: ModelStabilityGatewayKeyProvisionOverlay,
) -> TenantIdentityPostgres:
    """The identity store the overlay describes, built by the tenant provision node's own builder."""
    return build_store(
        ModelTenantProvisionOverlay(
            lane=overlay.lane, realms=(), database=overlay.database
        )
    )


__all__ = [
    "NODE_NAME",
    "ModelStabilityGatewayKeyProvisionOverlay",
    "build_identity_store",
    "load_overlay",
]
