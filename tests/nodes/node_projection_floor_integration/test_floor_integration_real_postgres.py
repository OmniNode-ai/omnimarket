# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real Postgres tests for the floor integration rows write path.

Catches: parameter binding (projected_at must bind as a datetime against
TIMESTAMPTZ, the OMN-15909 class); a newer pass not deleting the keys it no
longer names; an older pass overwriting or re-inserting rows; and the
migration's CHECK constraints not holding.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus

import asyncpg
import pytest

from omnimarket.models.floor_projection import ModelFloorProjectionEnvelope
from omnimarket.models.integration_reading import (
    ModelIntegrationProjection,
    ModelIntegrationReadingBatch,
)
from omnimarket.nodes.node_integration_projection_compute.handlers import (
    HandlerIntegrationProjection,
)
from omnimarket.nodes.node_projection_floor_integration.handlers.handler_floor_integration_writer import (
    FloorIntegrationProjectionWriter,
    upsert_statement,
)
from tests.nodes.node_integration_reading_publish_effect.test_integration_reading_publish import (
    _batch,
)

_SCHEMA = "omn25103_floor_integration_write_path_test"
EVENT = "onex.evt.omnimarket.floor-integration-pass.v1"


def _dsn_or_skip() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD not set -- skipping floor integration write-path DB proof"
        )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


async def _connect_or_skip() -> asyncpg.Connection:
    dsn = _dsn_or_skip()
    try:
        return await asyncpg.connect(dsn)
    except (
        OSError,
        asyncpg.PostgresError,
    ) as exc:
        pytest.skip(
            f"no reachable Postgres for floor integration write-path proof: {exc}"
        )


async def _prepare() -> None:
    conn = await _connect_or_skip()
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.execute(f"CREATE SCHEMA {_SCHEMA}")
        migration = (
            Path(__file__).resolve().parents[3]
            / "src/omnimarket/nodes/node_projection_floor_integration/migrations"
            / "0000_create_floor_integration_rows.sql"
        )
        await conn.execute(
            migration.read_text().replace("omninode_internal.", f"{_SCHEMA}.")
        )
    finally:
        await conn.close()


async def _drop() -> None:
    conn = await _connect_or_skip()
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    finally:
        await conn.close()


async def _served() -> list[asyncpg.Record]:
    conn = await _connect_or_skip()
    try:
        return list(
            await conn.fetch(
                f"SELECT key, state, seen_running, as_of, projected_at FROM {_SCHEMA}.floor_integration_rows"
            )
        )
    finally:
        await conn.close()


class _AsyncpgFloorIntegrationStore:
    """The writer's store port over a real asyncpg connection, pointed at the test schema."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn: asyncpg.Connection | None = None

    async def connect(self) -> None:
        self._conn = await asyncpg.connect(self._dsn)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def execute(self, query: str, *params: object) -> list[dict[str, object]]:
        if self._conn is None:
            raise RuntimeError("_AsyncpgFloorIntegrationStore is not connected")
        rows = await self._conn.fetch(
            query.replace("omninode_internal.", f"{_SCHEMA}."), *params
        )
        return [dict(row) for row in rows]


def _envelope(
    batch: ModelIntegrationReadingBatch,
) -> tuple[ModelIntegrationProjection, dict[str, object]]:
    projection = HandlerIntegrationProjection().handle(batch)
    payload = ModelFloorProjectionEnvelope(
        projection_id="integration",
        as_of=projection.as_of,
        producer="real-postgres-test",
        payload=projection.model_dump(mode="json"),
    ).model_dump(mode="json")
    return projection, payload


@pytest.mark.integration
def test_real_postgres_writer_holds_the_newest_pass() -> None:
    """One pass in, a newer pass that drops a key, then the older pass again."""
    dsn = _dsn_or_skip()
    asyncio.run(_prepare())
    try:
        writer = FloorIntegrationProjectionWriter(
            store=_AsyncpgFloorIntegrationStore(dsn)
        )
        first_batch = _batch()
        newer_batch = first_batch.model_copy(
            update={
                "as_of": first_batch.as_of + timedelta(hours=1),
                "readings": first_batch.readings[1:],
            }
        )
        first, first_payload = _envelope(first_batch)
        newer, newer_payload = _envelope(newer_batch)

        result = writer.handle(
            {**first_payload, "_topic": EVENT, "_partition": 0, "_offset": 0}
        )
        assert result["rows_upserted"] == len(first.rows)
        rows = asyncio.run(_served())
        assert {r["key"] for r in rows} == set(first.rows)
        assert all(isinstance(r["projected_at"], datetime) for r in rows)
        assert all(r["projected_at"].tzinfo is not None for r in rows)

        writer.handle({**newer_payload, "_topic": EVENT, "_partition": 0, "_offset": 1})
        rows = asyncio.run(_served())
        assert {r["key"] for r in rows} == set(newer.rows)
        assert len({r["as_of"] for r in rows}) == 1

        stale = writer.handle(
            {**first_payload, "_topic": EVENT, "_partition": 0, "_offset": 2}
        )
        assert stale.get("skipped_older_pass") is True
        assert {r["key"] for r in asyncio.run(_served())} == set(newer.rows)
    finally:
        asyncio.run(_drop())


@pytest.mark.integration
def test_real_postgres_refuses_a_non_positive_change_number() -> None:
    dsn = _dsn_or_skip()
    asyncio.run(_prepare())

    async def insert_bad() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(
                upsert_statement(f"{_SCHEMA}.floor_integration_rows"),
                "omnimarket#0",
                "omnimarket",
                0,
                "t",
                "2026-11-18T00:00:00+00:00",
                "dev",
                "running",
                "none",
                "",
                "",
                "",
                1,
                1,
                0,
                0,
                0,
                "2026-11-18T00:00:00+00:00",
                datetime.now().astimezone(),
            )
        finally:
            await conn.close()

    try:
        with pytest.raises(asyncpg.CheckViolationError):
            asyncio.run(insert_bad())
    finally:
        asyncio.run(_drop())
