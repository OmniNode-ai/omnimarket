# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Explicit registry-backed tenants for delegation projection fixtures."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import UUID

from omnimarket.projection.protocol_database import DatabaseAdapter
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from omnimarket.projection.tenant_registry_resolution import (
    TENANT_REGISTRY_MIRROR_TABLE,
)

PROJECTION_TENANT_SLUG = "delegation-projection-test"
PROJECTION_TENANT_UUID = UUID("00000000-0000-4000-8000-000000020651")


def seed_tenant_registry(db: DatabaseAdapter) -> str:
    """Seed the same mirror the sync writer reads, including SQLite stores."""
    if isinstance(db, SqliteDatabaseAdapter):
        db.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db.db_path) as conn:
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {TENANT_REGISTRY_MIRROR_TABLE} "
                "(tenant_slug TEXT PRIMARY KEY, tenant_uuid TEXT NOT NULL, "
                "status TEXT NOT NULL, source_event_id TEXT NOT NULL)"
            )
    db.upsert(
        TENANT_REGISTRY_MIRROR_TABLE,
        "tenant_slug",
        {
            "tenant_slug": PROJECTION_TENANT_SLUG,
            "tenant_uuid": str(PROJECTION_TENANT_UUID),
            "status": "active",
            "source_event_id": "c0000000-0000-4000-8000-000000020651",
        },
    )
    return PROJECTION_TENANT_SLUG


def mock_tenant_registry(db: Mock) -> None:
    """Answer only the async writer's mirror lookup for the declared tenant."""

    async def fetchval(query: str, *params: object, **kwargs: object) -> UUID | None:
        if (
            query.startswith(f"SELECT tenant_uuid FROM {TENANT_REGISTRY_MIRROR_TABLE}")
            and params
            and params[0] in {PROJECTION_TENANT_SLUG, PROJECTION_TENANT_UUID}
        ):
            return PROJECTION_TENANT_UUID
        return None

    db.fetchval = AsyncMock(side_effect=fetchval)


def mirror_tenant_in_store(db_path: Path, tenant_identity: str) -> str:
    """Record a caller-named tenant in a local evidence store's own mirror.

    OMN-22221: a delegation whose tenant is not a row of the evidence target's
    ``tenant_registry_mirror`` is refused at its start, so a test that is about
    something else (routing, keys, models) and names a customer tenant must
    mirror that tenant first, as the registry projection does in a deployment.
    An identity the registry or the closed legacy mapping already resolves is
    left alone, so a legacy slug never gains a row that disagrees with its map.
    """
    from datetime import UTC, datetime
    from uuid import uuid5

    from omnimarket.handlers.local_identity_mirror import write_mirror_row
    from omnimarket.local_deployment.tenant_identity import _ensure_tables_on
    from omnimarket.projection.tenant_registry_resolution import (
        TenantRegistryResolutionError,
        resolve_registry_tenant_uuid,
        sync_registry_tenant_uuid,
    )

    adapter = SqliteDatabaseAdapter(db_path)
    _ensure_tables_on(adapter)
    try:
        resolve_registry_tenant_uuid(
            tenant_identity,
            registry_uuid=sync_registry_tenant_uuid(adapter, tenant_identity),
        )
    except TenantRegistryResolutionError:
        pass
    else:
        return tenant_identity
    try:
        tenant_uuid = UUID(tenant_identity)
        slug = f"mirrored-{tenant_uuid.hex[:12]}"
    except ValueError:
        slug = tenant_identity
        tenant_uuid = uuid5(UUID(int=0), tenant_identity)
    write_mirror_row(
        adapter, tenant_uuid=tenant_uuid, tenant_slug=slug, now=datetime.now(tz=UTC)
    )
    return tenant_identity
