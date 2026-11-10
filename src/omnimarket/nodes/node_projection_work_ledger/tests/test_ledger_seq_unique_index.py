# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 of OMN-23152: a ledger_seq is held by one row, a unique index refuses a second, backfill keeps file order.

Golden chain: a row carrying a free sequence is inserted. Error chain: a second row for a held
sequence is recorded as a conflict and never inserted, and the consumer is not poisoned. The fake
database enforces the unique index as Postgres does (it raises on a second holder), so a writer
that skipped its own collision check fails here the way it would fail on the lab. The migration
text is read for the index, the dedupe of the already-duplicated sequence and the kept-row rule.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest

from omnimarket.nodes.node_projection_work_ledger.handlers import (
    handler_work_ledger_projection as writer_module,
)
from omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_projection import (
    WorkLedgerProjectionWriter,
)
from omnimarket.nodes.node_projection_work_ledger.handlers.work_ledger_seq_backfill import (
    SeqBackfillRefusedError,
    plan_seq_backfill,
)
from omnimarket.projection.runner import MessageMeta

pytestmark = pytest.mark.unit

MIGRATIONS = Path(writer_module.__file__).resolve().parent.parent / "migrations"
DUPLICATED_SEQ = 190463
ROW_A = "2026-11-04T10:00:00Z | STATUS | lane=judge | ticket=OMN-1 | first row of the sequence"
ROW_B = "2026-11-04T10:00:01Z | FRICTION | lane=hook | ticket=OMN-1 | second row claiming it"
TOPIC_STATUS = "onex.evt.omnimarket.work-ledger-status.v1"
TOPIC_FRICTION = "onex.evt.omnimarket.work-ledger-friction.v1"


class UniqueViolationError(Exception):
    """What Postgres raises when the unique index refuses a second holder."""


class _FakeConnection:
    """Enough SQL surface for the writer: holder lookup, row insert, conflict insert, state ops."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.conflicts: dict[str, dict[str, Any]] = {}

    @asynccontextmanager
    async def transaction(self) -> Any:
        yield

    async def fetchval(self, sql: str, *args: Any) -> Any:
        ledger_id, seq, row_id = args
        for held in self.rows.values():
            if (
                held["ledger_id"] == ledger_id
                and held["ledger_seq"] == seq
                and held["row_id"] != row_id
            ):
                return held["row_id"]
        return None

    async def execute(self, sql: str, *args: Any) -> None:
        if sql.lstrip().startswith(
            "INSERT INTO omninode_internal.work_ledger_seq_conflicts"
        ):
            row_id, _ledger_id, seq, held, raw, _source, reason = args
            self.conflicts.setdefault(
                row_id,
                {
                    "ledger_seq": seq,
                    "held_row_id": held,
                    "raw_row": raw,
                    "reason": reason,
                },
            )
        elif sql.lstrip().startswith("INSERT INTO omninode_internal.work_ledger_rows"):
            row_id, ledger_id, _ts, _type, _lane, _tickets, raw, _source, _at, seq = (
                args
            )
            for held in self.rows.values():
                if (
                    seq is not None
                    and held["ledger_id"] == ledger_id
                    and held["ledger_seq"] == seq
                    and held["row_id"] != row_id
                ):
                    raise UniqueViolationError(
                        f"ledger_seq {seq} is held by {held['row_id']}"
                    )
            self.rows.setdefault(
                row_id,
                {
                    "row_id": row_id,
                    "ledger_id": ledger_id,
                    "ledger_seq": seq,
                    "raw": raw,
                },
            )


class _Pool:
    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    @asynccontextmanager
    async def acquire(self) -> Any:
        yield self._connection


class _FakeDb:
    dsn = ""

    def __init__(self, connection: _FakeConnection) -> None:
        self.pool = _Pool(connection)


class _FakeDbWriter(WorkLedgerProjectionWriter):
    def __init__(self, connection: _FakeConnection) -> None:
        super().__init__()
        self._db = cast("Any", _FakeDb(connection))


def _writer() -> tuple[WorkLedgerProjectionWriter, _FakeConnection]:
    connection = _FakeConnection()
    return _FakeDbWriter(connection), connection


def _meta(topic: str) -> MessageMeta:
    return MessageMeta(partition=0, offset=1, fallback_id="omn23152", topic=topic)


async def _project(
    writer: WorkLedgerProjectionWriter, topic: str, row: str, seq: int | None
) -> None:
    await writer.project_event(topic, {"raw_row": row, "ledger_seq": seq}, _meta(topic))


@pytest.mark.asyncio
async def test_ledger_seq_unique_index_golden_a_free_sequence_is_inserted() -> None:
    writer, connection = _writer()
    await _project(writer, TOPIC_STATUS, ROW_A, DUPLICATED_SEQ)
    assert [r["ledger_seq"] for r in connection.rows.values()] == [DUPLICATED_SEQ]
    assert connection.conflicts == {}


@pytest.mark.asyncio
async def test_ledger_seq_unique_index_error_a_second_holder_is_a_conflict_not_a_poison() -> (
    None
):
    writer, connection = _writer()
    await _project(writer, TOPIC_STATUS, ROW_A, DUPLICATED_SEQ)
    await _project(writer, TOPIC_FRICTION, ROW_B, DUPLICATED_SEQ)
    assert len(connection.rows) == 1
    (conflict,) = connection.conflicts.values()
    assert conflict["ledger_seq"] == DUPLICATED_SEQ
    assert conflict["raw_row"] == ROW_B
    assert conflict["held_row_id"] in connection.rows
    assert conflict["reason"] == writer_module.SEQ_CONFLICT_REASON


@pytest.mark.asyncio
async def test_ledger_seq_unique_index_redelivery_of_the_holder_is_not_a_conflict() -> (
    None
):
    writer, connection = _writer()
    await _project(writer, TOPIC_STATUS, ROW_A, DUPLICATED_SEQ)
    await _project(writer, TOPIC_STATUS, ROW_A, DUPLICATED_SEQ)
    assert connection.conflicts == {}
    assert len(connection.rows) == 1


@pytest.mark.asyncio
async def test_ledger_seq_unique_index_injected_break_without_the_writer_check_poisons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Named break: with the writer's collision check removed, the index raises, which is the poison."""
    writer, _ = _writer()
    await _project(writer, TOPIC_STATUS, ROW_A, DUPLICATED_SEQ)

    async def no_holder(self: _FakeConnection, sql: str, *args: Any) -> None:
        return None

    monkeypatch.setattr(_FakeConnection, "fetchval", no_holder)
    with pytest.raises(UniqueViolationError):
        await _project(writer, TOPIC_FRICTION, ROW_B, DUPLICATED_SEQ)


def test_ledger_seq_unique_index_migration_declares_the_index_and_the_dedupe() -> None:
    sql = (MIGRATIONS / "0009_work_ledger_seq_unique.sql").read_text()
    index = re.search(
        r"CREATE UNIQUE INDEX IF NOT EXISTS (\w+)\s+ON omninode_internal\.work_ledger_rows"
        r"\s*\(ledger_id, ledger_seq\)\s+WHERE ledger_seq IS NOT NULL",
        sql,
    )
    assert index is not None
    # The dedupe runs before the index: a table holding 190463 twice would refuse the index.
    assert sql.index("rank_in_seq > 1") < sql.index("CREATE UNIQUE INDEX")
    assert "work_ledger_seq_conflicts" in sql
    assert re.search(r"\bDELETE\s+FROM\b", sql, re.IGNORECASE) is None
    grant = (MIGRATIONS / "0010_grant_work_ledger_seq_conflicts.sql").read_text()
    granted = grant.split("GRANT SELECT", 1)[1].split("TO", 1)[0]
    assert "INSERT, UPDATE" in granted
    assert "DELETE" not in granted


def test_ledger_seq_unique_index_backfill_keeps_file_order() -> None:
    file_order = ["a", "b", "c", "d", "e"]
    plan = plan_seq_backfill(file_order, {"a": 1, "d": 4})
    assert plan == {"b": 2, "c": 3, "e": 5}
    assert plan_seq_backfill(["x", "y"], {}, first_seq=10) == {"x": 10, "y": 11}


@pytest.mark.parametrize(
    ("file_order", "held"),
    [
        (["a", "b"], {"a": 5, "b": 3}),  # held sequences disagree with file order
        (["a", "b", "c"], {"a": 1, "c": 5}),  # two rows cannot fill a gap of three
        (["a", "a"], {}),  # a row twice in the file
        (["a"], {"zz": 1}),  # a held row the file does not hold
    ],
)
def test_ledger_seq_unique_index_backfill_refuses_a_guess(
    file_order: list[str], held: dict[str, int]
) -> None:
    with pytest.raises(SeqBackfillRefusedError):
        plan_seq_backfill(file_order, held)
