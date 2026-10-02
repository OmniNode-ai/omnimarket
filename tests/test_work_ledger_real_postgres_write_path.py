# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19513: real-Postgres write-path gate for the work-ledger projection.

A database double accepts a bound parameter of any Python type: an ISO ``str``
binds as "successfully" as a ``datetime`` into a ``TIMESTAMPTZ`` column. Only a
real Postgres connection enforces column types through asyncpg's extended query
protocol (the OMN-15905 defect class). So this file proves what nothing else can:

1. the migration DDL is valid and the writer's SQL binds against it;
2. ``is_open`` is GENERATED from the two column groups and nothing can write it;
3. the ``ON CONFLICT ... WHERE (at, row_id) <= EXCLUDED`` guard refuses an
   out-of-order redelivery IN SQL;
4. a redelivered row is ``ON CONFLICT DO NOTHING`` on the log.

It SKIPS (never ERRORs) without a reachable database and provisions a throwaway
schema so concurrent runs never collide. Signal: ``INTEGRATION_POSTGRES``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.events.enum_ledger_row_type import (
    EnumLedgerRowType,
)
from omnimarket.nodes.node_projection_work_ledger.handlers import (
    handler_work_ledger_projection as writer_module,
)
from omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_projection import (
    WorkLedgerProjectionWriter,
)
from omnimarket.projection.runner import MessageMeta

pytestmark = pytest.mark.integration

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_work_ledger/migrations"
    / "0000_create_work_ledger.sql"
)
CLAIM = "2026-09-28T10:00:00Z | CLAIM | lane=alpha | ticket=OMN-1 | est ~1 lane-hours; displaces x; (OMN-1) | work"
TERMINAL = (
    "2026-09-28T10:30:00Z | TERMINAL | lane=alpha | ticket=OMN-1 | friction=none | done"
)
CLAIM_LATER = "2026-09-28T11:00:00Z | CLAIM | lane=alpha | ticket=OMN-2 | est ~1 lane-hours; displaces x; (OMN-2) | again"
HOLD = "2026-09-28T10:10:00Z | HOLD | lane=beta | id=2026-09-28T10:10:00Z-beta | surface=lab-dev | until=2026-09-28T12:00:00Z | reserved"


def _dsn() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


async def _connect_or_skip() -> asyncpg.Connection:
    if not os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    ):
        pytest.skip(
            "no Postgres password set -- skipping the OMN-19513 write-path gate"
        )
    try:
        return await asyncpg.connect(_dsn())
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for the OMN-19513 write-path gate: {exc}")
    raise AssertionError("unreachable")


class _SingleConnectionAcquire:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self._connection = connection

    async def __aenter__(self) -> asyncpg.Connection:
        return self._connection

    async def __aexit__(self, *exc: object) -> None:
        return None


class _SingleConnectionPool:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self._connection = connection

    def acquire(self) -> _SingleConnectionAcquire:
        return _SingleConnectionAcquire(self._connection)


class _ConnectionDb:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self._connection = connection

    @property
    def pool(self) -> _SingleConnectionPool:
        return _SingleConnectionPool(self._connection)

    async def execute(self, sql: str, *args: Any) -> None:
        await self._connection.execute(sql, *args)

    async def connect(self) -> None: ...

    async def close(self) -> None: ...


@asynccontextmanager
async def _migrated() -> AsyncIterator[
    tuple[WorkLedgerProjectionWriter, asyncpg.Connection, str]
]:
    connection = await _connect_or_skip()
    schema = f"omn19513_{uuid4().hex[:12]}"
    names = ("_INSERT_ROW", "_OPEN_ENTITY", "_CLOSE_ENTITY")
    originals = {n: getattr(writer_module, n) for n in names}
    try:
        await connection.execute(f"CREATE SCHEMA {schema}")
        await connection.execute(
            MIGRATION.read_text().replace("omninode_internal.", f"{schema}.")
        )
        writer = WorkLedgerProjectionWriter()
        writer._db = _ConnectionDb(connection)  # type: ignore[assignment]
        for name, sql in originals.items():
            setattr(
                writer_module, name, sql.replace("omninode_internal.", f"{schema}.")
            )
        yield writer, connection, schema
    finally:
        for name, sql in originals.items():
            setattr(writer_module, name, sql)
        await connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await connection.close()


def _meta(topic: str) -> MessageMeta:
    return MessageMeta(partition=0, offset=1, fallback_id="omn19513", topic=topic)


async def _project(
    writer: WorkLedgerProjectionWriter, row_type: EnumLedgerRowType, raw: str
) -> None:
    await writer.project_event(row_type.topic, {"raw_row": raw}, _meta(row_type.topic))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_ddl_is_valid_and_a_claim_lifecycle_binds_and_closes() -> None:
    async with _migrated() as (writer, connection, schema):
        await _project(writer, EnumLedgerRowType.CLAIM, CLAIM)
        row = await connection.fetchrow(f"SELECT * FROM {schema}.work_ledger_state")
        assert row is not None
        assert row["entity_key"] == "claim:alpha"
        assert row["is_open"] is True
        await _project(writer, EnumLedgerRowType.TERMINAL, TERMINAL)
        row = await connection.fetchrow(
            f"SELECT is_open FROM {schema}.work_ledger_state"
        )
        assert row is not None
        assert row["is_open"] is False
        await _project(writer, EnumLedgerRowType.CLAIM, CLAIM_LATER)
        row = await connection.fetchrow(
            f"SELECT is_open, ticket FROM {schema}.work_ledger_state"
        )
        assert row is not None
        assert row["is_open"] is True
        assert row["ticket"] == "OMN-2"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_older_opening_row_cannot_overwrite_a_newer_one_in_sql() -> None:
    async with _migrated() as (writer, connection, schema):
        await _project(writer, EnumLedgerRowType.CLAIM, CLAIM_LATER)
        await _project(writer, EnumLedgerRowType.CLAIM, CLAIM)
        row = await connection.fetchrow(
            f"SELECT ticket FROM {schema}.work_ledger_state"
        )
        assert row is not None
        assert row["ticket"] == "OMN-2"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_redelivered_row_is_one_log_row() -> None:
    async with _migrated() as (writer, connection, schema):
        await _project(writer, EnumLedgerRowType.HOLD, HOLD)
        await _project(writer, EnumLedgerRowType.HOLD, HOLD)
        assert (
            await connection.fetchval(f"SELECT count(*) FROM {schema}.work_ledger_rows")
            == 1
        )
        assert (
            await connection.fetchval(
                f"SELECT count(*) FROM {schema}.work_ledger_state"
            )
            == 1
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_is_open_is_generated_and_cannot_be_written() -> None:
    async with _migrated() as (writer, connection, schema):
        await _project(writer, EnumLedgerRowType.CLAIM, CLAIM)
        with pytest.raises(asyncpg.PostgresError):
            await connection.execute(
                f"UPDATE {schema}.work_ledger_state SET is_open = false"
            )
