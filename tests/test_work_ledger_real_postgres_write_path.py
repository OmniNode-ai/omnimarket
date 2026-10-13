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
4. a redelivered row only fills a previously NULL ledger sequence on the log.

It SKIPS (never ERRORs) without a reachable database and provisions a throwaway
schema so concurrent runs never collide. Signal: ``INTEGRATION_POSTGRES``.
"""

from __future__ import annotations

import os
import sys
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
from omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_write_guard import (
    LedgerTestWriteRefusedError,
)
from omnimarket.projection.runner import MessageMeta

pytestmark = pytest.mark.integration

# A non-loopback host like the .201 runtime's DSN (host postgres). The test double
# never connects through it; the guard only reads it.
REAL_HOST_DSN = "postgresql://role_runtime@postgres:5432/omninode"

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
    def __init__(self, connection: asyncpg.Connection, dsn: str = "") -> None:
        self._connection = connection
        self.dsn = dsn

    @property
    def pool(self) -> _SingleConnectionPool:
        return _SingleConnectionPool(self._connection)

    async def execute(self, sql: str, *args: Any) -> None:
        await self._connection.execute(sql, *args)

    async def connect(self) -> None: ...

    async def close(self) -> None: ...


@asynccontextmanager
async def _migrated(
    dsn: str = "",
) -> AsyncIterator[tuple[WorkLedgerProjectionWriter, asyncpg.Connection, str]]:
    connection = await _connect_or_skip()
    schema = f"omn19513_{uuid4().hex[:12]}"
    names = ("_INSERT_ROW", "_OPEN_ENTITY", "_CLOSE_ENTITY")
    originals = {n: getattr(writer_module, n) for n in names}
    try:
        await connection.execute(f"CREATE SCHEMA {schema}")
        await connection.execute(
            MIGRATION.read_text().replace("omninode_internal.", f"{schema}.")
        )
        await connection.execute(
            (MIGRATION.parent / "0003_work_ledger_seq.sql")
            .read_text()
            .replace("omninode_internal.", f"{schema}.")
        )
        writer = WorkLedgerProjectionWriter()
        writer._db = _ConnectionDb(connection, dsn)  # type: ignore[assignment]
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_runtime_shape_writes_with_pytest_imported_and_no_test_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OMN-17427: the runtime imports pytest but sets no test env; its write must land, not stop the process."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("ONEX_TEST_CONTEXT", raising=False)
    assert "pytest" in sys.modules
    async with _migrated(REAL_HOST_DSN) as (writer, connection, schema):
        await _project(writer, EnumLedgerRowType.CLAIM, CLAIM)
        count = await connection.fetchval(
            f"SELECT count(*) FROM {schema}.work_ledger_rows"
        )
        assert count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_test_context_write_to_a_real_host_is_refused_without_system_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OMN-19513 still holds: under a test signal a real-host DSN writes nothing, and the refusal is an ordinary error."""
    monkeypatch.setenv("ONEX_TEST_CONTEXT", "1")
    async with _migrated(REAL_HOST_DSN) as (writer, connection, schema):
        with pytest.raises(LedgerTestWriteRefusedError):
            await _project(writer, EnumLedgerRowType.CLAIM, CLAIM)
        count = await connection.fetchval(
            f"SELECT count(*) FROM {schema}.work_ledger_rows"
        )
        assert count == 0


@pytest.mark.asyncio
async def test_ledger_seq_replay_fills_null_and_never_overwrites() -> None:
    async with _migrated() as (writer, connection, schema):
        topic = EnumLedgerRowType.CLAIM.topic
        for ledger_seq, expected in ((None, None), (7, 7), (99, 7), (None, 7)):
            await writer.project_event(
                topic, {"raw_row": CLAIM, "ledger_seq": ledger_seq}, _meta(topic)
            )
            stored = await connection.fetchval(
                f"SELECT ledger_seq FROM {schema}.work_ledger_rows"
            )
            assert stored == expected
        assert (
            await connection.fetchval(f"SELECT count(*) FROM {schema}.work_ledger_rows")
            == 1
        )


@pytest.mark.asyncio
async def test_ledger_seq_window_presence_includes_rows_outside_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from omnimarket.handlers.work_ledger_seq_gap import PostgresWorkLedgerSeqReader
    from omnimarket.nodes.node_work_ledger_seq_gap_effect import (
        HandlerWorkLedgerSeqGap,
    )
    from omnimarket.nodes.node_work_ledger_seq_gap_effect.models import (
        ModelWorkLedgerSeqGapRequest,
    )

    async with _migrated() as (_writer, connection, schema):
        # The window spans seqs 2..5; seq 3 exists outside its timestamp bounds.
        # Seq 4 is missing, seq 5 is duplicated, and an unsequenced row is newest.
        for row_id, seq, timestamp in (
            ("a", 1, "2026-10-07T12:00:00+00:00"),
            ("b", 2, "2026-10-08T01:00:00+00:00"),
            ("c", 3, "2026-10-07T12:00:00+00:00"),
            ("d", 5, "2026-10-08T02:00:00+00:00"),
            ("e", 5, "2026-10-07T12:00:00+00:00"),
            ("f", None, "2026-10-08T03:00:00+00:00"),
            ("g", 100, "2026-10-08T01:00:00+00:00"),
        ):
            await connection.execute(
                f"""INSERT INTO {schema}.work_ledger_rows
                    (row_id, ledger_id, row_ts, row_type, raw_row, projected_at, ledger_seq)
                    VALUES ($1, $2, $3, 'STATUS', $1, $3, $4)""",
                row_id,
                "other-ledger" if row_id == "g" else "rolling-work-ledger",
                datetime.fromisoformat(timestamp),
                seq,
            )
        monkeypatch.setenv("WORK_LEDGER_SEQ_INTEGRATION_DSN", _dsn())
        reader = PostgresWorkLedgerSeqReader(
            "WORK_LEDGER_SEQ_INTEGRATION_DSN", f"{schema}.work_ledger_rows"
        )
        report = HandlerWorkLedgerSeqGap(reader).handle(
            ModelWorkLedgerSeqGapRequest(
                correlation_id=uuid4(),
                since=datetime(2026, 10, 8, tzinfo=UTC),
                until=datetime(2026, 10, 8, 23, 59, 59, tzinfo=UTC),
            )
        )
        assert (report.from_seq, report.to_seq) == (2, 5)
        assert report.first_missing_seq == 4
        assert report.missing_count == 1
        assert report.contiguous_through == 3
        assert report.duplicate_seqs == (5,)
        assert report.duplicate_count == 1
        assert report.rows_with_seq == 4
        assert report.rows_without_seq == 1
        assert report.max_seq_row_ts == datetime(2026, 10, 8, 2, tzinfo=UTC)
        assert report.newest_row_ts == datetime(2026, 10, 8, 3, tzinfo=UTC)
