# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21217: the credential registry rebuilds from its state topic on real Postgres.

``tests/test_omn21217_credential_state_topic.py`` proves the SQL text and the
publish calls with a mock database. Only a real database proves what a replay
actually leaves behind, so this file drives the real runner against disposable
schemas (the harness of ``tests/test_omn16316_real_postgres_tenant_credentials_write_path.py``)
and SKIPS, never errors, without a reachable database. It runs in the pull
request's hosted CI.

Failure modes, each with a test below:
  P1  replaying the published credential-state rows into an empty registry does
      not reproduce the registry they came from
  P2  an older state row read after a newer one overwrites it
  P3  a state row read after a revoke brings the credential back
  P4  a register leaves the key's own ordered models off its catalog row, where
      routing reads them, or a refused version-conflict replace changes them
  P5  the migration recovers no choice for an existing key, or recovers one
      for a key whose routes disagree
  P6  replaying the published route rows into an empty overlay does not
      reproduce the routes they came from, a revoke's un-pointing included
  P7  an older route row read after a newer one overwrites it
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import quote_plus
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_projection import (
    HandlerTenantCredentialsProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.snapshot_publisher import encode_snapshot_delta

_NODES = Path(__file__).resolve().parents[1] / "src" / "omnimarket" / "nodes"
_MIGRATIONS = (
    *sorted(
        (_NODES / "node_projection_tenant_credentials" / "migrations").glob("*.sql")
    ),
    *sorted((_NODES / "node_delegation_routing_reducer" / "migrations").glob("*.sql")),
)

TOPIC_REGISTERED = "onex.evt.omnimarket.credential-registered.v1"
TOPIC_REVOKED = "onex.evt.omnimarket.credential-revoked.v1"
STATE_TOPIC = "onex.evt.omnimarket.tenant-credential-state.v1"
OVERLAY_TOPIC = "onex.evt.omnimarket.tenant-routing-overlay-state.v1"
TENANT = "omn21217-state-tenant"
_COMPARED = (
    "api_key_ref",
    "tenant_id",
    "name",
    "provider",
    "fingerprint",
    "expires_at",
    "bound_host",
    "workspace_id",
    "models",
    "version",
)


_ROUTE_COMPARED = (
    "task_type",
    "backend_id",
    "provider",
    "endpoint_url",
    "model_name",
    "secret_ref",
    "version",
)


class _Published(list[Any]):
    """The credential-state messages a runner published; ``routes``, its route rows."""

    def __init__(self) -> None:
        super().__init__()
        self.routes: list[Any] = []


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
        pytest.skip("POSTGRES_PASSWORD not set -- skipping OMN-21217 replay proof")
    try:
        return await asyncpg.connect(_base_dsn())
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"no reachable Postgres for OMN-21217 replay proof: {exc}")


@asynccontextmanager
async def _registry() -> AsyncIterator[
    tuple[HandlerTenantCredentialsProjectionRunner, asyncpg.Connection, _Published]
]:
    """A runner over a fresh schema; the third item collects its state publishes."""
    admin_conn = await _connect_or_skip()
    schema = f"omn21217_{uuid4().hex[:16]}"
    pool: asyncpg.Pool | None = None
    published = _Published()
    try:
        await admin_conn.execute(f"CREATE SCHEMA {schema}")
        await admin_conn.execute(f"SET search_path TO {schema}, public")
        for migration in _MIGRATIONS:
            await admin_conn.execute(migration.read_text(encoding="utf-8"))
        pool = await asyncpg.create_pool(
            _base_dsn(),
            min_size=1,
            max_size=3,
            server_settings={"search_path": f"{schema},public"},
        )
        adapter = AsyncpgAdapter(dsn=_base_dsn())
        adapter._pool = pool
        runner = HandlerTenantCredentialsProjectionRunner()
        runner._db = adapter

        async def record(exposure: Any, **kwargs: Any) -> bool:
            sink = {STATE_TOPIC: published, OVERLAY_TOPIC: published.routes}.get(
                exposure.topic
            )
            if sink is not None:
                sink.append(
                    encode_snapshot_delta(
                        exposure, observed_at="2026-10-25T00:00:00+00:00", **kwargs
                    )
                )
            return True

        runner.publish_snapshot_delta = AsyncMock(side_effect=record)
        yield runner, admin_conn, published
    finally:
        if pool is not None:
            with contextlib.suppress(Exception):
                await pool.close()
        with contextlib.suppress(Exception):
            await admin_conn.execute("SET search_path TO public")
            await admin_conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin_conn.close()


def _meta(topic: str, offset: int) -> MessageMeta:
    return MessageMeta(
        partition=0, offset=offset, fallback_id=f"f-{offset}", topic=topic
    )


async def _register(
    runner: Any,
    ref: str,
    name: str,
    offset: int,
    models: list[str] | None = None,
    expected_version: int | None = None,
) -> None:
    chosen = models or ["vendor/a"]
    assert await runner.project_event(
        TOPIC_REGISTERED,
        {
            "tenant_id": TENANT,
            "provider": "openrouter",
            "name": name,
            "api_key_ref": ref,
            "models": chosen,
            "metadata": {"model": chosen[0]},
            "expected_version": expected_version,
        },
        _meta(TOPIC_REGISTERED, offset),
    )


async def _replay(runner: Any, messages: list[Any]) -> None:
    for offset, message in enumerate(messages):
        assert await runner.project_event(
            message.topic, json.loads(message.value), _meta(message.topic, offset)
        )


async def _routes(conn: asyncpg.Connection) -> dict[str, dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT * FROM delegation_routing_tenant_overlay WHERE tenant_id = $1", TENANT
    )
    return {
        row["task_type"]: {
            **{column: row[column] for column in _ROUTE_COMPARED},
            "models": json.loads(row["models"])
            if isinstance(row["models"], str)
            else row["models"],
        }
        for row in rows
    }


async def _rows(conn: asyncpg.Connection) -> dict[str, dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT * FROM tenant_inference_credentials WHERE tenant_id = $1", TENANT
    )
    return {
        row["api_key_ref"]: {
            **{column: row[column] for column in _COMPARED},
            "models": None if row["models"] is None else json.loads(row["models"]),
            "revoked": row["revoked_at"] is not None,
        }
        for row in rows
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_p1_replaying_the_state_topic_rebuilds_the_registry() -> None:
    async with _registry() as (source, source_conn, published):
        await _register(source, "ref-a", "key a", 0, ["vendor/a", "vendor/b"])
        await _register(source, "ref-b", "key b", 1)
        await _register(source, "ref-a", "key a renamed", 2, ["vendor/b", "vendor/a"])
        assert await source.project_event(
            TOPIC_REVOKED,
            {"tenant_id": TENANT, "api_key_ref": "ref-b"},
            _meta(TOPIC_REVOKED, 3),
        )
        expected = await _rows(source_conn)
        messages = list(published)

    # Two registers, each also recording the key's choices with the route.
    assert expected["ref-a"]["version"] == 4
    assert expected["ref-a"]["models"] == ["vendor/b", "vendor/a"]
    assert expected["ref-b"]["revoked"] is True

    async with _registry() as (replica, replica_conn, republished):
        await _replay(replica, messages)
        assert await _rows(replica_conn) == expected
        # Applying a state row never publishes it again.
        assert republished == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_p2_an_older_state_row_never_overwrites_a_newer_one() -> None:
    async with _registry() as (source, _conn, published):
        await _register(source, "ref-a", "first name", 0)
        await _register(source, "ref-a", "second name", 1)
        older, newer = published[0], published[-1]

    async with _registry() as (replica, replica_conn, _republished):
        await _replay(replica, [newer, older])
        row = (await _rows(replica_conn))["ref-a"]
        assert row["version"] == 4
        assert row["name"] == "second name"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_p3_a_state_row_read_after_a_revoke_never_brings_it_back() -> None:
    async with _registry() as (source, _conn, published):
        await _register(source, "ref-a", "key a", 0)
        assert await source.project_event(
            TOPIC_REVOKED,
            {"tenant_id": TENANT, "api_key_ref": "ref-a"},
            _meta(TOPIC_REVOKED, 1),
        )
        live, revoked = published[-2], published[-1]

    async with _registry() as (replica, replica_conn, _republished):
        await _replay(replica, [revoked, live])
        row = (await _rows(replica_conn))["ref-a"]
        assert row["revoked"] is True
        assert row["version"] == 3


@pytest.mark.integration
@pytest.mark.asyncio
async def test_p4_the_keys_models_live_on_its_row_and_follow_only_applied_replaces() -> (
    None
):
    async with _registry() as (runner, conn, _published):
        await _register(runner, "ref-a", "key a", 0, ["vendor/a", "vendor/b"])
        # Routing reads the key's choices from its catalog row.
        assert (await _rows(conn))["ref-a"]["models"] == ["vendor/a", "vendor/b"]
        route = await conn.fetchrow(
            "SELECT version FROM delegation_routing_tenant_overlay WHERE tenant_id = $1",
            TENANT,
        )
        await _register(
            runner, "ref-a", "key a", 1, ["vendor/c"], expected_version=route["version"]
        )
        # A second replace on the same, now stale, version is refused.
        await _register(
            runner, "ref-a", "key a", 2, ["vendor/d"], expected_version=route["version"]
        )
        assert (await _rows(conn))["ref-a"]["models"] == ["vendor/c"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_p5_the_migration_recovers_only_an_unambiguous_choice() -> None:
    async with _registry() as (_runner, conn, _published):
        for ref in ("ref-one", "ref-split", "ref-none"):
            await conn.execute(
                "INSERT INTO tenant_inference_credentials "
                "(api_key_ref, tenant_id, name, provider, created_at) "
                "VALUES ($1, $2, 'k', 'openrouter', NOW())",
                ref,
                TENANT,
            )
        for task_type, ref, model in (
            ("t1", "ref-one", "vendor/a"),
            ("t1b", "ref-one", "vendor/a"),
            ("t2", "ref-split", "vendor/a"),
            ("t3", "ref-split", "vendor/b"),
        ):
            await conn.execute(
                "INSERT INTO delegation_routing_tenant_overlay "
                "(tenant_id, task_type, backend_id, provider, endpoint_url, "
                "model_name, secret_ref, created_at, updated_at) "
                "VALUES ($1, $2, 'b', 'openrouter', 'https://e.invalid', $3, $4, "
                "NOW(), NOW())",
                TENANT,
                task_type,
                model,
                ref,
            )
        migration = next(
            path
            for path in _MIGRATIONS
            if path.name.startswith("0009_tenant_credentials")
        )
        # Rerunnable: applied once by the fixture, applied again here.
        await conn.execute(migration.read_text(encoding="utf-8"))
        rows = await _rows(conn)
        assert rows["ref-one"]["models"] == ["vendor/a"]
        assert rows["ref-split"]["models"] is None
        assert rows["ref-none"]["models"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_p6_replaying_the_overlay_topic_rebuilds_the_routes() -> None:
    async with _registry() as (source, source_conn, published):
        await _register(source, "ref-a", "key a", 0, ["vendor/a", "vendor/b"])
        await _register(source, "ref-b", "key b", 1, ["vendor/c"])
        assert await source.project_event(
            TOPIC_REVOKED,
            {"tenant_id": TENANT, "api_key_ref": "ref-b"},
            _meta(TOPIC_REVOKED, 2),
        )
        expected = await _routes(source_conn)
        credentials = await _rows(source_conn)
        messages = [*published, *published.routes]

    # The revoke un-pointed the tenant's route from the key it named.
    assert expected["*"]["secret_ref"] is None
    assert expected["*"]["version"] == 3

    async with _registry() as (replica, replica_conn, republished):
        await _replay(replica, messages)
        assert await _routes(replica_conn) == expected
        assert await _rows(replica_conn) == credentials
        assert list(republished) == []
        assert republished.routes == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_p7_an_older_route_row_never_overwrites_a_newer_one() -> None:
    async with _registry() as (source, _conn, published):
        await _register(source, "ref-a", "key a", 0, ["vendor/a"])
        await _register(source, "ref-a", "key a", 1, ["vendor/b"])
        older, newer = published.routes

    async with _registry() as (replica, replica_conn, _republished):
        await _replay(replica, [newer, older])
        route = (await _routes(replica_conn))["*"]
        assert route["version"] == 2
        assert route["models"] == ["vendor/b"]
