# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Credential registration and revoke-first writes assign versions on real Postgres.

An undeclared provider keeps registration on the catalog path, isolating its
version increments from the separate route model writes.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote_plus
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_store import (
    apply_credential_registered,
    apply_credential_revoked,
)
from omnimarket.projection.postgres_sync_database import PostgresSyncProjectionAdapter

pytestmark = pytest.mark.integration

_NODES = Path(__file__).resolve().parents[1] / "src" / "omnimarket" / "nodes"
_MIGRATIONS = [
    *sorted(
        (_NODES / "node_projection_tenant_credentials" / "migrations").glob("*.sql")
    ),
    # Revocation reads the routing overlay and tenant identity mirror.
    *sorted((_NODES / "node_delegation_routing_reducer" / "migrations").glob("*.sql")),
    _NODES
    / "node_projection_tenant_registry"
    / "migrations"
    / "0000_create_tenant_registry_mirror.sql",
]

SLUG = "omn22182-credential-version-tenant"


def _dsn() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


@asynccontextmanager
async def _schema() -> AsyncIterator[tuple[asyncpg.Connection, str]]:
    if not os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    ):
        pytest.skip("POSTGRES_PASSWORD not set -- skipping the real-Postgres proof")
    try:
        conn = await asyncpg.connect(_dsn())
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"no reachable Postgres for the real-Postgres proof: {exc}")
    schema = f"omn22182_{uuid4().hex[:16]}"
    try:
        await conn.execute(f"CREATE SCHEMA {schema}")
        await conn.execute(f"SET search_path TO {schema}, public")
        for migration in _MIGRATIONS:
            await conn.execute(migration.read_text(encoding="utf-8"))
        yield conn, schema
    finally:
        with contextlib.suppress(Exception):
            await conn.execute("SET search_path TO public")
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()


async def _version(conn: asyncpg.Connection, ref: str) -> int:
    value = await conn.fetchval(
        "SELECT version FROM tenant_inference_credentials"
        " WHERE tenant_id = $1 AND api_key_ref = $2",
        SLUG,
        ref,
    )
    assert isinstance(value, int)
    return value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_registration_assigns_and_increments_version_real_postgres() -> None:
    async with _schema() as (conn, schema):
        ref = f"cred_{SLUG}_{uuid4().hex}"
        event = {
            "tenant_id": SLUG,
            # No declared backend means no route model write increments the version.
            "provider": "omn22182_unrouted",
            "name": "omn22182",
            "api_key_ref": ref,
            "models": ["vendor/a"],
        }

        def adapter() -> PostgresSyncProjectionAdapter:
            return PostgresSyncProjectionAdapter(_dsn(), schema=schema)

        assert await asyncio.to_thread(apply_credential_registered, event, adapter())
        assert await _version(conn, ref) == 1
        assert await asyncio.to_thread(apply_credential_registered, event, adapter())
        assert await _version(conn, ref) == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unknown_revoke_assigns_and_increments_version_real_postgres() -> None:
    async with _schema() as (conn, schema):
        ref = f"cred_{SLUG}_{uuid4().hex}"
        event = {"api_key_ref": ref, "tenant_id": SLUG}

        def adapter() -> PostgresSyncProjectionAdapter:
            return PostgresSyncProjectionAdapter(_dsn(), schema=schema)

        assert await asyncio.to_thread(apply_credential_revoked, event, adapter())
        assert await _version(conn, ref) == 1
        assert await asyncio.to_thread(apply_credential_revoked, event, adapter())
        assert await _version(conn, ref) == 2
