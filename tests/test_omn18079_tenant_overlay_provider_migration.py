# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-Postgres schema compatibility for OMN-18079 overlay provenance."""

from __future__ import annotations

import contextlib
import os
import subprocess
from pathlib import Path
from urllib.parse import quote_plus
from uuid import uuid4

import asyncpg
import pytest

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MIGRATIONS = (
    _REPO_ROOT / "src/omnimarket/nodes/node_delegation_routing_reducer/migrations"
)
_CREATE_MIGRATION = "0001_create_delegation_routing_tenant_overlay.sql"
_PROVIDER_MIGRATION = "0002_add_delegation_routing_tenant_overlay_provider.sql"


def _dsn() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    database = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{database}"


async def _connect_or_skip() -> asyncpg.Connection:
    if not os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    ):
        pytest.skip(
            "POSTGRES_PASSWORD not set -- skipping OMN-18079 real-Postgres migration gate"
        )
    try:
        return await asyncpg.connect(_dsn())
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infrastructure
        pytest.skip(f"no reachable Postgres for OMN-18079 migration gate: {exc}")


async def _column_exists(conn: asyncpg.Connection, schema: str) -> bool:
    return bool(
        await conn.fetchval(
            "SELECT EXISTS ("
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = $1 AND table_name = $2 AND column_name = $3"
            ")",
            schema,
            "delegation_routing_tenant_overlay",
            "provider",
        )
    )


async def _in_schema() -> tuple[asyncpg.Connection, str]:
    conn = await _connect_or_skip()
    schema = f"omn18079_{uuid4().hex[:16]}"
    await conn.execute(f'CREATE SCHEMA "{schema}"')
    await conn.execute(f'SET search_path TO "{schema}", public')
    return conn, schema


@pytest.mark.asyncio
async def test_fresh_schema_applies_create_then_additive_provider_migration() -> None:
    conn, schema = await _in_schema()
    try:
        for path in sorted(_MIGRATIONS.glob("*.sql")):
            await conn.execute(path.read_text(encoding="utf-8"))
        assert await _column_exists(conn, schema)
    finally:
        with contextlib.suppress(Exception):
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.close()


@pytest.mark.asyncio
async def test_existing_pre_provider_row_is_preserved_by_forward_migration() -> None:
    conn, schema = await _in_schema()
    try:
        old_create = subprocess.check_output(
            [
                "git",
                "show",
                f"HEAD:{_MIGRATIONS.relative_to(_REPO_ROOT) / _CREATE_MIGRATION}",
            ],
            cwd=_REPO_ROOT,
            text=True,
        )
        await conn.execute(old_create)
        await conn.execute(
            "INSERT INTO delegation_routing_tenant_overlay "
            "(tenant_id, task_type, backend_id, endpoint_url, model_name) "
            "VALUES ('legacy-tenant', 'legacy-task', 'legacy-route', 'https://example.invalid/v1', 'legacy-model')"
        )
        assert not await _column_exists(conn, schema)

        await conn.execute(
            (_MIGRATIONS / _PROVIDER_MIGRATION).read_text(encoding="utf-8")
        )

        assert await _column_exists(conn, schema)
        row = await conn.fetchrow(
            "SELECT backend_id, provider FROM delegation_routing_tenant_overlay "
            "WHERE tenant_id = 'legacy-tenant'"
        )
        assert row is not None
        assert row["backend_id"] == "legacy-route"
        assert row["provider"] is None
    finally:
        with contextlib.suppress(Exception):
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.close()
