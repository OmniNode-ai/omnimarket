# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Revoking a key blanks its route whichever tenant form stored it, on real Postgres (OMN-21819).

The revocation write path reads the routing overlay by tenant. A route stamped
with the tenant's registry UUID must be blanked when the revoke names the tenant
by its slug (and the other way round), through one unique mirror row; without a
mirror row the read stays exact and the route is left alone. Real Postgres, so
the overlay and mirror column types are the deployed ones, not a mock's.
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
    *sorted((_NODES / "node_delegation_routing_reducer" / "migrations").glob("*.sql")),
    _NODES
    / "node_projection_tenant_registry"
    / "migrations"
    / "0000_create_tenant_registry_mirror.sql",
]

SLUG = "omn21819-tenant"
TENANT_UUID = "6f2b1c3e-0d4a-4f5e-9a8b-7c6d5e4f3a2b"


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
    schema = f"omn21819_{uuid4().hex[:16]}"
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


async def _routes_left(conn: asyncpg.Connection, ref: str) -> int:
    value = await conn.fetchval(
        "SELECT count(*) FROM delegation_routing_tenant_overlay WHERE secret_ref = $1",
        ref,
    )
    assert isinstance(value, int)
    return value


def _registered(tenant: str, ref: str) -> dict[str, object]:
    return {
        "tenant_id": tenant,
        "provider": "openrouter",
        "name": "omn21819",
        "api_key_ref": ref,
        "models": ["vendor/a"],
        "metadata": {"model": "vendor/a"},
    }


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "revoked_as"), [(TENANT_UUID, SLUG), (SLUG, TENANT_UUID)]
)
async def test_revoke_blanks_the_route_stored_in_the_other_form_real_postgres(
    stored: str, revoked_as: str
) -> None:
    async with _schema() as (conn, schema):
        await conn.execute(
            "INSERT INTO tenant_registry_mirror (tenant_slug, tenant_uuid, status)"
            " VALUES ($1, $2::uuid, 'active')",
            SLUG,
            TENANT_UUID,
        )
        ref = f"cred_{SLUG}_{uuid4().hex}"

        def adapter() -> PostgresSyncProjectionAdapter:
            return PostgresSyncProjectionAdapter(_dsn(), schema=schema)

        await asyncio.to_thread(
            apply_credential_registered, _registered(stored, ref), adapter()
        )
        assert await _routes_left(conn, ref) >= 1
        await asyncio.to_thread(
            apply_credential_revoked,
            {"api_key_ref": ref, "tenant_id": revoked_as},
            adapter(),
        )
        assert await _routes_left(conn, ref) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_revoke_without_a_mirror_row_keeps_the_exact_read_real_postgres() -> None:
    async with _schema() as (conn, schema):
        ref = f"cred_{SLUG}_{uuid4().hex}"

        def adapter() -> PostgresSyncProjectionAdapter:
            return PostgresSyncProjectionAdapter(_dsn(), schema=schema)

        await asyncio.to_thread(
            apply_credential_registered, _registered(TENANT_UUID, ref), adapter()
        )
        before = await _routes_left(conn, ref)
        assert before >= 1
        await asyncio.to_thread(
            apply_credential_revoked, {"api_key_ref": ref, "tenant_id": SLUG}, adapter()
        )
        assert await _routes_left(conn, ref) == before


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("revoked_as", [SLUG, TENANT_UUID])
async def test_revoke_blanks_routes_stored_in_both_forms_real_postgres(
    revoked_as: str,
) -> None:
    async with _schema() as (conn, schema):
        await conn.execute(
            "INSERT INTO tenant_registry_mirror (tenant_slug, tenant_uuid, status)"
            " VALUES ($1, $2::uuid, 'active')",
            SLUG,
            TENANT_UUID,
        )
        ref = f"cred_{SLUG}_{uuid4().hex}"

        def adapter() -> PostgresSyncProjectionAdapter:
            return PostgresSyncProjectionAdapter(_dsn(), schema=schema)

        await asyncio.to_thread(
            apply_credential_registered, _registered(SLUG, ref), adapter()
        )
        # The same route as the writer's later stage would stamp it: the
        # registry UUID in place of the name, every other column the same.
        columns = [
            row["column_name"]
            for row in await conn.fetch(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = $1"
                " AND table_name = 'delegation_routing_tenant_overlay'"
                " AND is_identity = 'NO' AND is_generated = 'NEVER'"
                " AND coalesce(column_default, '') NOT LIKE 'nextval(%'"
                " ORDER BY ordinal_position",
                schema,
            )
        ]
        selected = ", ".join(
            "$1::text" if column == "tenant_id" else column for column in columns
        )
        await conn.execute(
            f"INSERT INTO delegation_routing_tenant_overlay ({', '.join(columns)})"
            f" SELECT {selected} FROM delegation_routing_tenant_overlay"
            " WHERE tenant_id = $2 AND secret_ref = $3",
            TENANT_UUID,
            SLUG,
            ref,
        )
        assert await _routes_left(conn, ref) == 2
        await asyncio.to_thread(
            apply_credential_revoked,
            {"api_key_ref": ref, "tenant_id": revoked_as},
            adapter(),
        )
        assert await _routes_left(conn, ref) == 0
