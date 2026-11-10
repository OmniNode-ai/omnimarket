# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22658: cached contract parse across real-Postgres runtime deliveries on an owned INTEGRATION_POSTGRES server.

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
from omnimarket.handlers import delivery_cache
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
def test_deliveries_parse_the_writer_contract_once_and_still_write_real_rows(
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
    delivery_cache._parse_contract.cache_clear()
    parses: list[int] = []
    real_safe_load = delivery_cache.yaml.safe_load

    def counting_safe_load(stream: Any) -> Any:
        parses.append(1)
        return real_safe_load(stream)

    monkeypatch.setattr(delivery_cache.yaml, "safe_load", counting_safe_load)
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
            # OMN-23196: the pool opened by the first delivery serves the rest.
            assert len(adapters) == 1
            assert all(adapter.is_connected for adapter in adapters)
        writer.close()
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
        # three deliveries built three runners and read the contract file once
        assert len(parses) == 1
    finally:
        asyncio.run(teardown())
