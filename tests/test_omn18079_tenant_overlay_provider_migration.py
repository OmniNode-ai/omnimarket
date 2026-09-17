# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-Postgres schema compatibility for OMN-18079 overlay provenance."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import NoReturn
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
_PROVIDER_MIGRATION = "0004_add_delegation_routing_tenant_overlay_provider.sql"


def test_provider_migration_is_append_only_and_fail_closed() -> None:
    migration = (_MIGRATIONS / _PROVIDER_MIGRATION).read_text(encoding="utf-8")

    assert "ADD COLUMN provider TEXT" in migration
    assert "IF NOT EXISTS" not in migration
    assert "delegation_routing_tenant_overlay_provider_token" in migration


def _skip_postgres_unavailable(reason: str) -> NoReturn:
    pytest.skip(reason)
    raise AssertionError("pytest.skip did not raise")


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
        _skip_postgres_unavailable(
            "POSTGRES_PASSWORD not set -- skipping OMN-18079 real-Postgres migration gate"
        )
    try:
        return await asyncpg.connect(_dsn())
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infrastructure
        _skip_postgres_unavailable(
            f"no reachable Postgres for OMN-18079 migration gate: {exc}"
        )


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
        # This fixture is the historical table shape that existed before the
        # additive provenance migration. The live 0001 migration is allowed to
        # gain convergence clauses over time; this test pins the upgrade path
        # from an already-deployed pre-provenance database.
        await conn.execute(
            """
            CREATE TABLE delegation_routing_tenant_overlay (
                id BIGSERIAL PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                task_type TEXT NOT NULL,
                backend_id TEXT NOT NULL,
                endpoint_url TEXT NOT NULL,
                model_name TEXT NOT NULL,
                secret_ref TEXT,
                timeout_ms INTEGER
                    CONSTRAINT delegation_routing_tenant_overlay_timeout_ms_positive
                    CHECK (timeout_ms IS NULL OR timeout_ms > 0),
                max_tokens INTEGER
                    CONSTRAINT delegation_routing_tenant_overlay_max_tokens_positive
                    CHECK (max_tokens IS NULL OR max_tokens > 0),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT delegation_routing_tenant_overlay_tenant_task_uq
                    UNIQUE (tenant_id, task_type)
            )
            """
        )
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


@pytest.mark.asyncio
async def test_provider_token_constraint_accepts_safe_tokens_only() -> None:
    conn, schema = await _in_schema()
    try:
        for path in sorted(_MIGRATIONS.glob("*.sql")):
            await conn.execute(path.read_text(encoding="utf-8"))

        await conn.execute(
            "INSERT INTO delegation_routing_tenant_overlay "
            "(tenant_id, task_type, backend_id, endpoint_url, model_name, provider) "
            "VALUES ('tenant-a', 'review', 'route-a', 'https://example.invalid/v1', "
            "'model-a', 'openrouter')"
        )

        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO delegation_routing_tenant_overlay "
                "(tenant_id, task_type, backend_id, endpoint_url, model_name, provider) "
                "VALUES ('tenant-b', 'review', 'route-b', 'https://example.invalid/v1', "
                "'model-b', 'Open Router')"
            )
    finally:
        with contextlib.suppress(Exception):
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.close()
