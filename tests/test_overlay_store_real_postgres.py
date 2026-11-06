# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22478: the overlay store against a real Postgres, through the projection runner.

A database double accepts a bound parameter of any type and does not keep JSON key order the way a real
column does or does not; only a real connection shows the migration is valid, the writer's SQL binds, the
guard refuses an older load in SQL and the document comes back with its keys in their order, which the flat
table parsers depend on. It SKIPS without a reachable database; signal ``INTEGRATION_POSTGRES_PASSWORD``.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote_plus
from uuid import uuid4

import asyncpg
import pytest
from pydantic import ValidationError

from omnimarket.models.overlay_store import ModelOverlayStoreRecord
from omnimarket.nodes.node_overlay_store_read_effect.handlers import compare_documents
from omnimarket.nodes.node_projection_overlay_store.handlers import (
    HandlerOverlayStoreProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta, PublishFn
from tests.unit.nodes.node_overlay_store.support import (
    NOW,
    SNAPSHOT_TOPIC,
    TABLE,
    record,
)

pytestmark = pytest.mark.integration

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_overlay_store/migrations"
    / "0000_create_overlay_store.sql"
)
META = MessageMeta(topic="t", partition=0, offset=1, fallback_id="f")


def _dsn(database: str | None = None) -> str:
    password = quote_plus(os.environ.get("INTEGRATION_POSTGRES_PASSWORD", ""))
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = database or os.environ.get("INTEGRATION_POSTGRES_DB", "postgres")
    return f"postgresql://{quote_plus(user)}:{password}@{host}:{port}/{db}"


class _Acquire:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self._connection = connection

    async def __aenter__(self) -> asyncpg.Connection:
        return self._connection

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Pool:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self._connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self._connection)


class _Db:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self.pool = _Pool(connection)


class _ConnectionRunner(HandlerOverlayStoreProjectionRunner):
    """The real runner, its writes on one throwaway connection and its publishes to the caller."""

    def __init__(self, connection: asyncpg.Connection, publish: PublishFn) -> None:
        super().__init__(publish_fn=publish)
        self._connection = connection

    def db_for(
        self, table: str, *, operation: Literal["read", "write"] = "write"
    ) -> Any:
        assert operation == "write", (table, operation)
        return _Db(self._connection)


@asynccontextmanager
async def _migrated() -> AsyncIterator[
    tuple[
        HandlerOverlayStoreProjectionRunner,
        asyncpg.Connection,
        list[tuple[str, bytes]],
    ]
]:
    if not os.environ.get("INTEGRATION_POSTGRES_PASSWORD"):
        pytest.skip(
            "no INTEGRATION_POSTGRES_PASSWORD: skipping the overlay store write path"
        )
    try:
        admin = await asyncpg.connect(_dsn())
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"no reachable Postgres for the overlay store write path: {exc}")
    # A throwaway database, so the migration and the store's statements run exactly as written.
    database = f"overlay_store_{uuid4().hex[:12]}"
    await admin.execute(f"CREATE DATABASE {database}")
    connection = await asyncpg.connect(_dsn(database))
    try:
        await connection.execute("CREATE SCHEMA IF NOT EXISTS omninode_internal")
        await connection.execute(MIGRATION.read_text(encoding="utf-8"))
        published: list[tuple[str, bytes]] = []

        async def publish(topic: str, value: bytes) -> None:
            published.append((topic, value))

        runner = _ConnectionRunner(connection, publish)
        yield runner, connection, published
    finally:
        await connection.close()
        await admin.execute(f"DROP DATABASE {database}")
        await admin.close()


async def _stored(connection: asyncpg.Connection) -> dict[str, Any]:
    row = await connection.fetchrow(
        "SELECT document FROM omninode_internal.overlay_store "
        "ORDER BY last_loaded_at DESC LIMIT 1"
    )
    assert row is not None
    return json.loads(row["document"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_load_is_stored_equal_with_its_key_order_and_published_as_stored() -> (
    None
):
    async with _migrated() as (runner, connection, published):
        loaded = record()
        assert await runner.project_event("t", loaded.model_dump(mode="json"), META)
        stored = await _stored(connection)
        compared = compare_documents(TABLE, stored)
        assert compared.equal, compared.differing
        assert compared.rows_compared == 2
        assert list(stored) == list(TABLE)
        assert list(stored["hosts"][0]) == list(TABLE["hosts"][0])
        topic, value = published[-1]
        assert topic == SNAPSHOT_TOPIC
        assert ModelOverlayStoreRecord.model_validate_json(value) == loaded


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_older_load_changes_nothing_and_the_newer_is_republished() -> None:
    async with _migrated() as (runner, connection, published):
        newer = record()
        older = record(
            {**TABLE, "bus_lane": "older"}, loaded_at=NOW - timedelta(hours=1)
        )
        await runner.project_event("t", newer.model_dump(mode="json"), META)
        await runner.project_event("t", older.model_dump(mode="json"), META)
        assert (await _stored(connection))["bus_lane"] == TABLE["bus_lane"]
        assert ModelOverlayStoreRecord.model_validate_json(published[-1][1]) == newer


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_audit_keeps_each_version_and_the_time_it_was_last_asserted() -> None:
    async with _migrated() as (runner, connection, _):
        first = record(loaded_at=NOW - timedelta(hours=2))
        again = record(loaded_at=NOW - timedelta(hours=1))
        changed = record({**TABLE, "bus_lane": "next"})
        for load in (first, again, changed):
            await runner.project_event("t", load.model_dump(mode="json"), META)
        rows = await connection.fetch(
            "SELECT digest, first_loaded_at, last_loaded_at "
            "FROM omninode_internal.overlay_store"
        )
        assert {r["digest"] for r in rows} == {first.digest, changed.digest}
        kept = next(r for r in rows if r["digest"] == first.digest)
        assert kept["first_loaded_at"] == first.loaded_at
        assert kept["last_loaded_at"] == again.loaded_at


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_malformed_load_raises_for_the_dead_letter_and_stores_nothing() -> None:
    async with _migrated() as (runner, connection, published):
        bad = {**record().model_dump(mode="json"), "digest": "0" * 64}
        with pytest.raises(ValidationError):
            await runner.project_event("t", bad, META)
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM omninode_internal.overlay_store"
            )
            == 0
        )
        assert published == []
