# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime writer SQL and pool lifetime on an owned INTEGRATION_POSTGRES server.

The existing native-Postgres fixture owns its Unix socket and cluster. Only
fixture DSN provisioning and snapshot transport are substituted: AsyncpgAdapter
and the registration writer execute their real SQL and enforce column types.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_live_events.handlers.handler_runtime_writer import (
    LiveEventsProjectionWriter,
)
from omnimarket.nodes.node_projection_registration.handlers.handler_runtime_writer import (
    RegistrationProjectionWriter,
)
from omnimarket.projection.runner import BaseProjectionRunner
from tests.test_omn15359_ac3_replay_real_postgres import (
    local_postgres as local_postgres_fixture,
)
from tests.test_omn19514_ticket_id_projection_real_postgres import _Postgres

local_postgres = local_postgres_fixture


@pytest.mark.integration
def test_runtime_deliveries_write_typed_rows_and_close_real_pools(
    local_postgres: tuple[_Postgres, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    pg, _ = local_postgres
    schema = f"omn21288_{uuid4().hex[:16]}"
    # Unix socket from the test-owned cluster, never an ambient service DSN.
    integration_postgres_dsn = (
        f"postgresql://postgres@/postgres?host={quote(pg.host, safe='')}"
        f"&search_path={schema},public"
    )
    migration = (
        Path(__file__).parents[1]
        / "src/omnimarket/nodes/node_projection_registration/migrations"
        / "0000_create_node_service_registry.sql"
    )
    adapters: list[AsyncpgAdapter] = []
    snapshots: list[dict[str, Any]] = []

    async def setup() -> None:
        connection = await asyncpg.connect(**pg.connect_kwargs())
        try:
            await connection.execute(f"CREATE SCHEMA {schema}")
            await connection.execute(f"SET search_path TO {schema},public")
            # gen_random_uuid is built into supported PostgreSQL versions;
            # this table uses no pgcrypto functions beyond that UUID default.
            ddl = migration.read_text().replace(
                "CREATE EXTENSION IF NOT EXISTS pgcrypto;", ""
            )
            await connection.execute(ddl)
        finally:
            await connection.close()

    async def connect_fixture_database(self: BaseProjectionRunner) -> None:
        # The hermetic suite independently exercises per-binding resolution and
        # wrong-principal refusal. This fixture supplies its isolated real DB.
        self._db = AsyncpgAdapter(dsn=integration_postgres_dsn)
        adapters.append(self._db)
        await self._db.connect()

    async def publish_snapshot(
        self: BaseProjectionRunner, exposure: object, **kwargs: Any
    ) -> bool:
        snapshots.append(kwargs)
        return True

    async def readback() -> list[asyncpg.Record]:
        connection = await asyncpg.connect(integration_postgres_dsn)
        try:
            return await connection.fetch("SELECT * FROM node_service_registry")
        finally:
            await connection.close()

    async def teardown() -> None:
        connection = await asyncpg.connect(**pg.connect_kwargs())
        try:
            await connection.execute(f"DROP SCHEMA {schema} CASCADE")
        finally:
            await connection.close()

    monkeypatch.setattr(
        BaseProjectionRunner, "_connect_standalone_databases", connect_fixture_database
    )
    monkeypatch.setattr(
        BaseProjectionRunner, "publish_snapshot_delta", publish_snapshot
    )
    asyncio.run(setup())
    try:
        writer = RegistrationProjectionWriter()
        node_id = str(uuid4())
        intro = {
            "_topic": "onex.evt.platform.node-introspection.v1",
            "_partition": 2,
            "_offset": 41,
            "node_id": node_id,
            "node_name": "fixture-runtime-node",
            "node_type": "effect",
            "current_state": "healthy",
            "endpoints": {"http": "http://fixture.invalid:8080"},
        }
        heartbeat = {
            "_topic": "onex.evt.platform.node-heartbeat.v1",
            "_partition": 2,
            "_offset": 42,
            "node_id": node_id,
            "uptime_seconds": 42.0,
        }
        for payload in (intro, heartbeat, {**heartbeat, "_offset": 43}):
            assert writer.handle(payload) == {"projected": True}
            assert all(not adapter.is_connected for adapter in adapters)
        rows = asyncio.run(readback())
        assert len(rows) == 1
        row = rows[0]
        assert row["service_name"] == "fixture-runtime-node"
        assert row["health_status"] == "healthy"
        assert row["uptime_seconds"] == 42
        assert isinstance(row["last_heartbeat_at"], datetime)
        metadata = json.loads(row["metadata"])
        assert metadata["node_id"] == node_id
        assert metadata["endpoints"] == {"http": "http://fixture.invalid:8080"}
        assert [snapshot["source_offset"] for snapshot in snapshots] == [41, 42, 43]
    finally:
        asyncio.run(teardown())


@pytest.mark.integration
def test_live_events_runtime_delivery_strips_runner_keys_and_keeps_coordinates(
    local_postgres: tuple[_Postgres, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    pg, _ = local_postgres
    schema = f"omn21288_{uuid4().hex[:16]}"
    # Unix socket from the test-owned cluster, never an ambient service DSN.
    integration_postgres_dsn = (
        f"postgresql://postgres@/postgres?host={quote(pg.host, safe='')}"
        f"&search_path={schema},public"
    )
    migration = (
        Path(__file__).parents[1]
        / "src/omnimarket/nodes/node_projection_live_events/migrations"
        / "0000_create_live_events.sql"
    )
    adapters: list[AsyncpgAdapter] = []
    snapshots: list[dict[str, Any]] = []

    async def setup() -> None:
        connection = await asyncpg.connect(**pg.connect_kwargs())
        try:
            await connection.execute(f"CREATE SCHEMA {schema}")
            await connection.execute(f"SET search_path TO {schema},public")
            ddl = migration.read_text().replace(
                "CREATE EXTENSION IF NOT EXISTS pgcrypto;", ""
            )
            await connection.execute(ddl)
        finally:
            await connection.close()

    async def connect_fixture_database(self: BaseProjectionRunner) -> None:
        self._db = AsyncpgAdapter(dsn=integration_postgres_dsn)
        adapters.append(self._db)
        await self._db.connect()

    async def publish_snapshot(
        self: BaseProjectionRunner, exposure: object, **kwargs: Any
    ) -> bool:
        snapshots.append(kwargs)
        return True

    async def readback() -> list[asyncpg.Record]:
        connection = await asyncpg.connect(integration_postgres_dsn)
        try:
            return await connection.fetch("SELECT * FROM live_events")
        finally:
            await connection.close()

    async def teardown() -> None:
        connection = await asyncpg.connect(**pg.connect_kwargs())
        try:
            await connection.execute(f"DROP SCHEMA {schema} CASCADE")
        finally:
            await connection.close()

    monkeypatch.setattr(
        BaseProjectionRunner, "_connect_standalone_databases", connect_fixture_database
    )
    monkeypatch.setattr(
        BaseProjectionRunner, "publish_snapshot_delta", publish_snapshot
    )
    asyncio.run(setup())
    try:
        writer = LiveEventsProjectionWriter()
        event_id = str(uuid4())
        delivery = {
            "_topic": "onex.evt.platform.node-heartbeat.v1",
            "_partition": 3,
            "_offset": 7,
            "_envelope_id": str(uuid4()),
            "_event_type": "node-heartbeat",
            "_tenant_id": "omninode",
            "event_id": event_id,
            "node_name": "fixture-runtime-node",
            "timestamp": "2026-10-27T12:00:00+00:00",
        }
        for payload in (delivery, {**delivery, "_offset": 8}):
            assert writer.handle(payload) == {"projected": True}
            assert all(not adapter.is_connected for adapter in adapters)
        rows = asyncio.run(readback())
        assert len(rows) == 1
        row = rows[0]
        assert row["event_id"] == event_id
        assert row["topic"] == "onex.evt.platform.node-heartbeat.v1"
        assert isinstance(row["timestamp"], datetime)
        # Runtime bookkeeping never reaches the stored raw payload.
        assert sorted(json.loads(row["payload"])) == [
            "event_id",
            "node_name",
            "timestamp",
        ]
        assert [
            (snapshot["source_partition"], snapshot["source_offset"])
            for snapshot in snapshots
        ] == [(3, 7), (3, 8)]
    finally:
        asyncio.run(teardown())


@pytest.mark.integration
def test_live_events_delegate_skill_list_source_projects_a_string_source_row(
    local_postgres: tuple[_Postgres, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """OMN-17427: ``source: ["claude-code"]`` on the delegate-skill command is a
    row with a string source, not a validation error that wedges the consumer."""
    pg, _ = local_postgres
    schema = f"omn17427_{uuid4().hex[:16]}"
    integration_postgres_dsn = (
        f"postgresql://postgres@/postgres?host={quote(pg.host, safe='')}"
        f"&search_path={schema},public"
    )
    migration = (
        Path(__file__).parents[1]
        / "src/omnimarket/nodes/node_projection_live_events/migrations"
        / "0000_create_live_events.sql"
    )

    async def setup() -> None:
        connection = await asyncpg.connect(**pg.connect_kwargs())
        try:
            await connection.execute(f"CREATE SCHEMA {schema}")
            await connection.execute(f"SET search_path TO {schema},public")
            ddl = migration.read_text().replace(
                "CREATE EXTENSION IF NOT EXISTS pgcrypto;", ""
            )
            await connection.execute(ddl)
        finally:
            await connection.close()

    async def connect_fixture_database(self: BaseProjectionRunner) -> None:
        self._db = AsyncpgAdapter(dsn=integration_postgres_dsn)
        await self._db.connect()

    async def publish_snapshot(
        self: BaseProjectionRunner, exposure: object, **kwargs: Any
    ) -> bool:
        return True

    async def readback() -> list[asyncpg.Record]:
        connection = await asyncpg.connect(integration_postgres_dsn)
        try:
            return await connection.fetch("SELECT * FROM live_events")
        finally:
            await connection.close()

    async def teardown() -> None:
        connection = await asyncpg.connect(**pg.connect_kwargs())
        try:
            await connection.execute(f"DROP SCHEMA {schema} CASCADE")
        finally:
            await connection.close()

    monkeypatch.setattr(
        BaseProjectionRunner, "_connect_standalone_databases", connect_fixture_database
    )
    monkeypatch.setattr(
        BaseProjectionRunner, "publish_snapshot_delta", publish_snapshot
    )
    asyncio.run(setup())
    try:
        event_id = str(uuid4())
        delivery = {
            "_topic": "onex.cmd.omnimarket.delegate-skill.v1",
            "_partition": 0,
            "_offset": 11,
            "_envelope_id": str(uuid4()),
            "_event_type": "delegate-skill",
            "_tenant_id": "omninode",
            "event_id": event_id,
            "source": ["claude-code"],
            "timestamp": "2026-10-28T17:14:00+00:00",
        }
        assert LiveEventsProjectionWriter().handle(delivery) == {"projected": True}
        rows = asyncio.run(readback())
        assert [row["source"] for row in rows] == ["claude-code"]
        assert rows[0]["topic"] == "onex.cmd.omnimarket.delegate-skill.v1"
    finally:
        asyncio.run(teardown())
