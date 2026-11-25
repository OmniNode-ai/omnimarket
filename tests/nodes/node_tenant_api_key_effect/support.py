# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders for the tenant key node tests: handlers over a scratch Postgres, commands, digests."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from omnimarket.models.overlay_documents.model_tenant_api_key_overlay import (
    ModelTenantApiKeyOverlay,
)
from omnimarket.nodes.node_tenant_api_key_effect.handlers.handler_tenant_api_key_register import (
    HandlerTenantApiKeyRegister,
)
from omnimarket.nodes.node_tenant_api_key_effect.handlers.handler_tenant_api_key_revoke import (
    HandlerTenantApiKeyRevoke,
)
from omnimarket.nodes.node_tenant_api_key_effect.handlers.store_tenant_api_key_postgres import (
    PostgresTenantApiKeyStore,
)
from omnimarket.nodes.node_tenant_api_key_effect.models import (
    ModelTenantApiKeyRegisterRequest,
    ModelTenantApiKeyRevokeRequest,
)

DSN_ENV = "TENANT_IDENTITY_DATABASE_DSN"
DEV_OVERLAY = ModelTenantApiKeyOverlay(lane="dev", dev_secret_store_environment="dev")
HOSTED_OVERLAY = ModelTenantApiKeyOverlay(lane="hosted", max_lifetime_days=90)


def digest(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def store_for(dsn: str) -> PostgresTenantApiKeyStore:
    return PostgresTenantApiKeyStore(dsn_env=DSN_ENV, environ={DSN_ENV: dsn})


def register_handler(
    dsn: str, overlay: ModelTenantApiKeyOverlay = DEV_OVERLAY
) -> HandlerTenantApiKeyRegister:
    return HandlerTenantApiKeyRegister(store=store_for(dsn), overlay=overlay)


def revoke_handler(dsn: str) -> HandlerTenantApiKeyRevoke:
    return HandlerTenantApiKeyRevoke(store=store_for(dsn))


def register_command(
    tenant_id: UUID,
    *,
    name: str = "laptop",
    key_hash: str | None = None,
    expires_at: datetime | None = None,
) -> ModelTenantApiKeyRegisterRequest:
    return ModelTenantApiKeyRegisterRequest(
        correlation_id=uuid4(),
        tenant_id=tenant_id,
        name=name,
        key_hash=key_hash if key_hash is not None else digest(f"{tenant_id}/{name}"),
        expires_at=expires_at,
    )


def revoke_command(
    tenant_id: UUID, credential_id: UUID
) -> ModelTenantApiKeyRevokeRequest:
    return ModelTenantApiKeyRevokeRequest(
        correlation_id=uuid4(), tenant_id=tenant_id, credential_id=credential_id
    )


def in_hours(hours: float) -> datetime:
    return datetime.now(UTC) + timedelta(hours=hours)
