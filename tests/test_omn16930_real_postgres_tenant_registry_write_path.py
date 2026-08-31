# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16930: the tenant-registry writer against a REAL Postgres.

The projection-write-path gate exists because a mock DB cannot catch a
str-vs-datetime column-type mismatch (OMN-15905). This writer has exactly that
exposure and it is not hypothetical: ``tenant_uuid`` is a UUID column fed a
``str``, ``registry_created_at`` is TIMESTAMPTZ fed a ``datetime`` parsed out of
an ISO-8601 string by pydantic. Both bind through asyncpg, which enforces column
types at the protocol level and would reject a wrong one. A mock returns
whatever it was told to.

There is a second reason to run this writer against a real database, specific to
this relation. Everything downstream treats ``tenant_registry_mirror`` as the
authority a migration resolves identity against, so its CONSTRAINTS are part of
the contract, not an implementation detail: the slug primary key, the unique
index on ``tenant_uuid`` that stops two slugs merging two tenants, and the
COALESCE that keeps provisioning history from being rewritten by a replay. None
of those exist in a mock.

This is the real-Postgres companion the projection-write-path-db-gate looks
for on any diff that touches this node's handler module -- including a
docstring-only correction to the handler's own def-B canon-shape wording
(OMN-14355 C-core: the checker literal-scans resolved handler source text for
the envelope type name, so even a docstring saying the handler does NOT
import it tripped the same scan a real import would).

Harness (``_connect_or_skip`` / disposable schema) matches
``tests/test_omn16316_real_postgres_tenant_credentials_write_path.py``. SKIPS
(never ERRORs) without a reachable database.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote_plus
from uuid import UUID, uuid4

import asyncpg
import pytest

from omnimarket.nodes.node_projection_tenant_registry.handlers.handler_tenant_registry_projection import (
    HandlerTenantRegistryProjectionRunner,
    TenantIdentityRebindingError,
)
from omnimarket.projection.runner import MessageMeta

_MIRROR_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_tenant_registry"
    / "migrations"
    / "0000_create_tenant_registry_mirror.sql"
)

_TOPIC = "onex.tenant.events"
_SLUG = "t-1lostguy1"
_UUID = UUID("e9c62089-2fe8-4190-8fc2-1c40b757b7b1")
_OTHER_UUID = UUID("91c74442-1233-4c97-b191-911a10346fdf")


def _base_dsn() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


async def _connect_or_skip() -> asyncpg.Connection:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip(
            "POSTGRES_PASSWORD not set -- skipping OMN-16930 real-Postgres "
            "tenant-registry write path"
        )
    try:
        return await asyncpg.connect(_base_dsn())
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for OMN-16930 write path: {exc}")


@asynccontextmanager
async def _mirror_schema() -> AsyncIterator[tuple[asyncpg.Connection, str]]:
    conn = await _connect_or_skip()
    schema = f"omn16930w_{uuid4().hex[:16]}"
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.execute(f"CREATE SCHEMA {schema}")
        await conn.execute(f"SET search_path TO {schema}, public")
        await conn.execute(_MIRROR_MIGRATION.read_text(encoding="utf-8"))
        yield conn, schema
    finally:
        with contextlib.suppress(Exception):
            await conn.execute("SET search_path TO public")
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()


class _SchemaScopedDb:
    """Routes the runner's writes through the disposable schema's connection."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def execute(self, sql: str, *args: object) -> list[dict[str, object]]:
        rows = await self._conn.fetch(sql, *args)
        return [dict(row) for row in rows]


def _envelope(
    *,
    tenant_uuid: UUID = _UUID,
    slug: str = _SLUG,
    status: str = "active",
    created_at: str = "2026-08-26T16:17:00+00:00",
    operation: str = "TENANT_CREATED",
) -> dict[str, object]:
    return {
        "operation": operation,
        "success": True,
        "correlation_id": "omn16930-realdb",
        "payload": {
            "tenant": {
                "tenant_id": str(tenant_uuid),
                "tenant_slug": slug,
                "name": slug,
                "status": status,
                "created_at": created_at,
                "plan_code": "beta",
            }
        },
    }


def _meta() -> MessageMeta:
    return MessageMeta(partition=0, offset=0, fallback_id="omn16930", topic=_TOPIC)


def _runner(conn: asyncpg.Connection) -> HandlerTenantRegistryProjectionRunner:
    runner = HandlerTenantRegistryProjectionRunner()
    runner._db = _SchemaScopedDb(conn)  # type: ignore[assignment]
    return runner


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tenant_created_lands_a_typed_row() -> None:
    """The column types bind for real -- a UUID column fed a str, a TIMESTAMPTZ
    column fed a datetime. This is the OMN-15905 class the gate exists for."""
    async with _mirror_schema() as (conn, _schema):
        assert await _runner(conn).project_event(_TOPIC, _envelope(), _meta())

        row = await conn.fetchrow(
            "SELECT * FROM tenant_registry_mirror WHERE tenant_slug = $1", _SLUG
        )
        assert row is not None
        assert row["tenant_uuid"] == _UUID  # a real UUID, not a string
        assert row["status"] == "active"
        assert row["registry_created_at"] is not None
        assert row["registry_created_at"].year == 2026
        assert row["observed_at"] is not None
        assert row["source_event_id"] == "omn16930-realdb"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_replay_refreshes_observed_at_but_not_provisioning_history() -> None:
    """COALESCE, proven against the database rather than asserted on SQL text.

    ``registry_created_at`` is when the tenant was provisioned; ``observed_at``
    is when this projection last saw it. A TENANT_UPDATED replay must move the
    second and never the first.
    """
    async with _mirror_schema() as (conn, _schema):
        runner = _runner(conn)
        await runner.project_event(_TOPIC, _envelope(), _meta())
        first = await conn.fetchrow(
            "SELECT registry_created_at, observed_at FROM tenant_registry_mirror "
            "WHERE tenant_slug = $1",
            _SLUG,
        )

        await runner.project_event(
            _TOPIC,
            _envelope(
                operation="TENANT_UPDATED",
                status="suspended",
                created_at="2030-01-01T00:00:00+00:00",
            ),
            _meta(),
        )
        second = await conn.fetchrow(
            "SELECT registry_created_at, observed_at, status FROM "
            "tenant_registry_mirror WHERE tenant_slug = $1",
            _SLUG,
        )

        assert second["registry_created_at"] == first["registry_created_at"]
        assert second["observed_at"] >= first["observed_at"]
        assert second["status"] == "suspended"
        assert (
            await conn.fetchval("SELECT count(*) FROM tenant_registry_mirror") == 1
        ), "an upsert must not have produced a second row for the same slug"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rebinding_a_slug_is_refused_and_leaves_the_row_untouched() -> None:
    """A refused write must change nothing -- proven by reading the row back,
    not by counting mock calls."""
    async with _mirror_schema() as (conn, _schema):
        runner = _runner(conn)
        await runner.project_event(_TOPIC, _envelope(), _meta())

        with pytest.raises(TenantIdentityRebindingError):
            await runner.project_event(
                _TOPIC, _envelope(tenant_uuid=_OTHER_UUID), _meta()
            )

        assert (
            await conn.fetchval(
                "SELECT tenant_uuid FROM tenant_registry_mirror WHERE tenant_slug = $1",
                _SLUG,
            )
            == _UUID
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_slugs_cannot_claim_one_tenant_uuid() -> None:
    """The unique index is a contract, not a nicety.

    Two slugs resolving to the same UUID would merge two tenants' rows during a
    conversion -- the cross-tenant reassignment OMN-15683 exists to prevent.
    """
    async with _mirror_schema() as (conn, _schema):
        runner = _runner(conn)
        await runner.project_event(_TOPIC, _envelope(), _meta())

        with pytest.raises(asyncpg.PostgresError):
            await runner.project_event(_TOPIC, _envelope(slug="an-alias-slug"), _meta())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_mirror_carries_no_rls() -> None:
    """AC2 at the writer's own boundary.

    An RLS-covered mirror is invisible to the migrate identity with
    ``app.tenant_id`` unset, which is the whole failure the mechanism replaces.
    """
    async with _mirror_schema() as (conn, _schema):
        flags = await conn.fetchrow(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE oid = 'tenant_registry_mirror'::regclass"
        )
        assert flags["relrowsecurity"] is False
        assert flags["relforcerowsecurity"] is False
