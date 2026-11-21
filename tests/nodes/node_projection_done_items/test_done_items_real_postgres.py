# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real Postgres tests for the done_items write path.

Catches: parameter binding and projected_at round-tripping; newer events
replacing older ones while older events do not; replayed events returning no
row; and malformed done_row_id values being rejected by the database.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote_plus

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_done_items.handlers.handler_done_items_writer import (
    DoneItemsProjectionWriter,
    upsert_statement,
)
from omnimarket.nodes.node_projection_done_items.ports.done_items_store import (
    PostgresDoneItemsStore,
)
from tests.nodes.node_projection_done_items.test_done_items_projection import (
    DONE1,
    DONE_TWO,
    V1,
    V2,
    _v1,
    _v2,
)

_SCHEMA = "omn25104_done_items_write_path_test"


def _dsn_or_skip() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD not set -- skipping done-items write-path DB proof"
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
        raise pytest.skip.Exception(
            f"no reachable Postgres for done-items write-path proof: {exc}"
        ) from exc


async def _setup(conn: asyncpg.Connection) -> None:
    await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await conn.execute(f"CREATE SCHEMA {_SCHEMA}")
    migration_path = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_projection_done_items/migrations/0000_create_done_items.sql"
    )
    sql = migration_path.read_text()
    await conn.execute(sql.replace("omninode_internal.", f"{_SCHEMA}."))


statement = upsert_statement(f"{_SCHEMA}.done_items")


async def _teardown(conn: asyncpg.Connection) -> None:
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    finally:
        await conn.close()


@pytest.mark.integration
async def test_real_postgres_done_item_binds_and_round_trips() -> None:
    conn = await _connect_or_skip()
    try:
        await _setup(conn)
        projected_at = datetime(2026, 11, 17, 10, 0, 5, tzinfo=UTC)

        row = await conn.fetchrow(
            statement,
            "OMN-1",
            "2026-11-17T10:00:00Z",
            "a" * 64,
            "alpha",
            None,
            None,
            projected_at,
        )
        assert row is not None

        record = await conn.fetchrow(
            f"SELECT projected_at FROM {_SCHEMA}.done_items WHERE ticket = $1",
            "OMN-1",
        )
        assert isinstance(record["projected_at"], datetime)
        assert record["projected_at"] == projected_at
    finally:
        await _teardown(conn)


@pytest.mark.integration
async def test_real_postgres_newer_done_event_replaces_older_does_not() -> None:
    conn = await _connect_or_skip()
    try:
        await _setup(conn)

        await conn.fetchrow(
            statement,
            "OMN-1",
            "2026-11-17T10:00:00Z",
            "a" * 64,
            "alpha",
            None,
            None,
            datetime(2026, 11, 17, 10, 0, 5, tzinfo=UTC),
        )
        newer = await conn.fetchrow(
            statement,
            "OMN-1",
            "2026-11-17T11:00:00Z",
            "b" * 64,
            "alpha",
            None,
            None,
            datetime(2026, 11, 17, 11, 0, 5, tzinfo=UTC),
        )
        assert newer is not None

        older = await conn.fetchrow(
            statement,
            "OMN-1",
            "2026-11-17T10:30:00Z",
            "c" * 64,
            "alpha",
            None,
            None,
            datetime(2026, 11, 17, 10, 30, 5, tzinfo=UTC),
        )
        assert older is None

        final = await conn.fetchrow(
            f"SELECT done_at, done_row_id FROM {_SCHEMA}.done_items WHERE ticket = $1",
            "OMN-1",
        )
        assert final["done_at"] == "2026-11-17T11:00:00Z"
        assert final["done_row_id"] == "b" * 64
    finally:
        await _teardown(conn)


@pytest.mark.integration
async def test_real_postgres_same_event_replayed_returns_no_row() -> None:
    conn = await _connect_or_skip()
    try:
        await _setup(conn)
        params = (
            "OMN-1",
            "2026-11-17T10:00:00Z",
            "a" * 64,
            "alpha",
            None,
            None,
            datetime(2026, 11, 17, 10, 0, 5, tzinfo=UTC),
        )

        await conn.fetch(statement, *params)
        second = await conn.fetch(statement, *params)
        assert second == []

        count = await conn.fetchval(f"SELECT COUNT(*) FROM {_SCHEMA}.done_items")
        assert count == 1
    finally:
        await _teardown(conn)


@pytest.mark.integration
async def test_real_postgres_refuses_a_malformed_row_id() -> None:
    conn = await _connect_or_skip()
    try:
        await _setup(conn)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.fetch(
                statement,
                "OMN-1",
                "2026-11-17T10:00:00Z",
                "not-a-sha",
                "alpha",
                None,
                None,
                datetime(2026, 11, 17, 10, 0, 5, tzinfo=UTC),
            )
    finally:
        await _teardown(conn)


class _AsyncpgDoneItemsStore(PostgresDoneItemsStore):
    """The runtime's own Postgres store over a real asyncpg pool.

    Only the schema name moves to the test schema, the same rewrite ``_setup``
    applies to the migration, so apply_rows's transaction is the code under test.
    """

    def __init__(self, dsn: str) -> None:
        super().__init__(AsyncpgAdapter(dsn, min_size=1, max_size=2))

    async def execute(self, query: str, *params: object) -> list[dict[str, object]]:
        return await super().execute(_in_test_schema(query), *params)

    async def apply_rows(
        self, statement: str, rows: list[tuple[object, ...]]
    ) -> list[dict[str, object]]:
        return await super().apply_rows(_in_test_schema(statement), rows)


def _in_test_schema(query: str) -> str:
    return query.replace("omninode_internal.", f"{_SCHEMA}.")


async def _prepare() -> None:
    conn = await _connect_or_skip()
    try:
        await _setup(conn)
    finally:
        await conn.close()


async def _served_rows() -> list[asyncpg.Record]:
    conn = await _connect_or_skip()
    try:
        return list(
            await conn.fetch(
                f"SELECT ticket, done_row_id, projected_at FROM {_SCHEMA}.done_items"
            )
        )
    finally:
        await conn.close()


async def _drop() -> None:
    await _teardown(await _connect_or_skip())


@pytest.mark.integration
def test_real_postgres_writer_projects_v1_and_v2_terminal_events() -> None:
    """The writer's own path, both envelopes, into real Postgres.

    Catches the OMN-15909 class on this writer: a projected_at that is not a
    datetime fails the TIMESTAMPTZ bind, and a v2 envelope that no longer parses
    writes nothing. The v2 copy of the same ledger row is a replay, so it must
    not write a second time.
    """
    dsn = _dsn_or_skip()
    asyncio.run(_prepare())
    try:
        writer = DoneItemsProjectionWriter(store=_AsyncpgDoneItemsStore(dsn))
        first = writer.handle(
            {**_v1(DONE1), "_topic": V1, "_partition": 0, "_offset": 0}
        )
        assert first["rows_upserted"] == 1
        replay = writer.handle(
            {**_v2(DONE1), "_topic": V2, "_partition": 0, "_offset": 1}
        )
        assert replay["rows_upserted"] == 0

        rows = asyncio.run(_served_rows())
        assert len(rows) == 1
        assert rows[0]["ticket"] == "OMN-1"
        assert isinstance(rows[0]["projected_at"], datetime)
        assert rows[0]["projected_at"].tzinfo is not None
    finally:
        asyncio.run(_drop())


async def _execute_in_test_db(statement: str) -> None:
    conn = await _connect_or_skip()
    try:
        await conn.execute(statement)
    finally:
        await conn.close()


@pytest.mark.integration
def test_real_postgres_failed_write_keeps_the_held_rows() -> None:
    """An event closing OMN-2 and OMN-4 whose OMN-4 write fails leaves only the held rows."""
    dsn = _dsn_or_skip()
    asyncio.run(_prepare())
    try:
        writer = DoneItemsProjectionWriter(store=_AsyncpgDoneItemsStore(dsn))
        writer.handle({**_v1(DONE1), "_topic": V1, "_partition": 0, "_offset": 0})
        held = sorted(tuple(r) for r in asyncio.run(_served_rows()))
        asyncio.run(
            _execute_in_test_db(
                f"CREATE FUNCTION {_SCHEMA}.fail_one() RETURNS trigger LANGUAGE plpgsql AS "
                "$$ BEGIN IF NEW.ticket = 'OMN-4' THEN "
                "RAISE EXCEPTION 'injected write failure'; END IF; RETURN NEW; END $$"
            )
        )
        asyncio.run(
            _execute_in_test_db(
                f"CREATE TRIGGER fail_one BEFORE INSERT ON {_SCHEMA}.done_items "
                f"FOR EACH ROW EXECUTE FUNCTION {_SCHEMA}.fail_one()"
            )
        )
        with pytest.raises(asyncpg.PostgresError, match="injected write failure"):
            writer.handle(
                {**_v1(DONE_TWO), "_topic": V1, "_partition": 0, "_offset": 1}
            )
        assert sorted(tuple(r) for r in asyncio.run(_served_rows())) == held
    finally:
        asyncio.run(_drop())
