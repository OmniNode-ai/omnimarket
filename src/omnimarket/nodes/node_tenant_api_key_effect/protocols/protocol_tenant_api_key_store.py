# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The port to the identity tables: SQL only, no decision."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from omnimarket.nodes.node_tenant_api_key_effect.models.model_tenant_api_key_row import (
    ModelTenantApiKeyRow,
)
from omnimarket.nodes.node_tenant_api_key_effect.models.model_tenant_state_reading import (
    ModelTenantStateReading,
)


class TenantApiKeyStoreUnavailableError(RuntimeError):
    """The identity database could not be reached or answered with an error; the message is a class name only."""


class TenantApiKeyStoreSchemaOutdatedError(TenantApiKeyStoreUnavailableError):
    """The key table has no `expires_at` column."""


class TenantApiKeyInsertConflictError(RuntimeError):
    """The insert hit a unique index (the active name or the digest); the caller rereads to tell which."""


@runtime_checkable
class ProtocolTenantApiKeyStore(Protocol):
    async def read_by_key_hash(self, key_hash: str) -> ModelTenantApiKeyRow | None: ...

    async def read_tenant_state(self, tenant_id: UUID) -> ModelTenantStateReading: ...

    async def insert_key(
        self, *, tenant_id: UUID, name: str, key_hash: str, expires_at: datetime | None
    ) -> tuple[UUID, datetime]: ...

    async def revoke_key(self, *, tenant_id: UUID, credential_id: UUID) -> bool: ...

    async def key_exists(self, *, tenant_id: UUID, credential_id: UUID) -> bool: ...

    async def close(self) -> None: ...


__all__: list[str] = [
    "ProtocolTenantApiKeyStore",
    "TenantApiKeyInsertConflictError",
    "TenantApiKeyStoreSchemaOutdatedError",
    "TenantApiKeyStoreUnavailableError",
]
