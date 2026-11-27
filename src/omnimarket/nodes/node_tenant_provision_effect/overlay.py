# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tenant provision overlay: every deployment fact the node needs, found by reference.

Found at ``<root>/node_tenant_provision_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``; the
package ships ``overlay.example.yaml`` with synthetic values. Nothing here has a default: a lane, a realm, a secret name
or a bound the overlay leaves out is refused. The database address is never in the overlay: ``dsn_secret`` is the logical
name the secret store resolves (``tenant_identity.database.dsn``), and no value is held here.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_tenant_provision_effect.handlers.tenant_identity_postgres import (
    TenantIdentityPostgres,
    TenantStoreUnavailableError,
)

NODE_NAME = "node_tenant_provision_effect"


class ModelTenantIdentityDatabaseOverlay(BaseModel):
    """Where the identity database's DSN is found, and the pool's bounds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dsn_secret: str = Field(min_length=1)
    secret_resolver_config_path: str = Field(min_length=1)
    max_pool_size: int = Field(ge=1)
    query_timeout_seconds: float = Field(gt=0)


class ModelTenantProvisionOverlay(BaseModel):
    """The whole overlay document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    realms: tuple[str, ...]
    database: ModelTenantIdentityDatabaseOverlay


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelTenantProvisionOverlay:
    """The overlay document, validated."""
    return load_structured_overlay(NODE_NAME, ModelTenantProvisionOverlay, environ)


def build_store(overlay: ModelTenantProvisionOverlay) -> TenantIdentityPostgres:
    """The store the overlay describes."""
    database = overlay.database

    async def dsn() -> str:
        from omnimarket.inference.secret_store_resolver import (
            secret_store_from_config_path,
        )

        store = secret_store_from_config_path(database.secret_resolver_config_path)
        value = await store.get_secret(database.dsn_secret)
        if not value:
            raise TenantStoreUnavailableError("database secret has no value")
        return str(value)

    return TenantIdentityPostgres(
        dsn_resolver=dsn,
        max_pool_size=database.max_pool_size,
        query_timeout_seconds=database.query_timeout_seconds,
    )


__all__ = [
    "NODE_NAME",
    "ModelTenantIdentityDatabaseOverlay",
    "ModelTenantProvisionOverlay",
    "build_store",
    "load_overlay",
]
