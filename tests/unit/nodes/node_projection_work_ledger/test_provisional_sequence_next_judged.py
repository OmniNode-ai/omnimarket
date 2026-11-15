# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A projection-assigned sequence never costs the judge its next sequence (OMN-24634, review of omnimarket#6306).

The judge owns ``ledger_seq``: it counts on its own, gapless, and states the number on the row it
accepts. A live row that reaches the projection with none is given the next number after the tip so
that it lands in order, but that number is PROVISIONAL. The projection owns the row, so the row moves
and the judge's number stands, the order the history backfill keeps (``plan_seq_backfill``: a held
sequence is never moved, the rows with none take what is left).

The fake connection below enforces the unique index on (ledger_id, ledger_seq) the way Postgres does
and records a refused sequence in ``work_ledger_seq_conflicts``. The judge is a counter the projection
cannot read, so tip+1 and the judge's next number collide exactly as they did on the lab.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, cast

import pytest

from omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_projection import (
    WorkLedgerProjectionWriter,
)
from omnimarket.nodes.node_work_ledger_emit_effect.handlers.row_parser import (
    parse_ledger_row,
)
from omnimarket.projection.runner import MessageMeta

pytestmark = pytest.mark.unit

TOPIC = "onex.evt.omnimarket.work-ledger-claim.v1"
LEDGER = "rolling-work-ledger"


def _claim(n: int) -> str:
    return f"2026-11-15T10:0{n}:00Z | CLAIM | lane=alpha{n} | ticket=OMN-{n} | est ~1 lane-hours; displaces x; (OMN-{n}) | work"


class _UniqueViolationError(Exception):
    pass


class _Connection:
    """The SQL surface of the writer, dispatched on the statement text, with the unique index enforced."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.conflicts: dict[str, int] = {}

    @asynccontextmanager
    async def transaction(self) -> Any:
        yield

    def _holder(self, seq: int, row_id: str) -> dict[str, Any] | None:
        for held in self.rows.values():
            if held["ledger_seq"] == seq and held["row_id"] != row_id:
                return held
        return None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if "COALESCE(MAX(ledger_seq)" in sql:
            seqs = [r["ledger_seq"] for r in self.rows.values() if r["ledger_seq"]]
            return (max(seqs) if seqs else 0) + 1
        if "ledger_seq_provisional FROM" in sql:
            (row_id,) = args
            return self.rows[row_id]["provisional"]
        if "ledger_seq = $2 AND row_id <> $3" in sql:
            _ledger_id, seq, row_id = args
            holder = self._holder(seq, row_id)
            return holder["row_id"] if holder else None
        if "SELECT ledger_seq FROM" in sql:
            (row_id,) = args
            return self.rows[row_id]["ledger_seq"] if row_id in self.rows else None
        return None

    async def execute(self, sql: str, *args: Any) -> None:
        head = sql.lstrip()
        if head.startswith("INSERT INTO omninode_internal.work_ledger_seq_conflicts"):
            self.conflicts.setdefault(args[0], args[2])
        elif head.startswith("INSERT INTO omninode_internal.work_ledger_rows"):
            row_id, seq = args[0], args[9]
            provisional = bool(args[10]) if len(args) > 10 else False
            if seq is not None and self._holder(seq, row_id) is not None:
                raise _UniqueViolationError(f"ledger_seq {seq} is held")
            existing = self.rows.get(row_id)
            if existing is None:
                self.rows[row_id] = {
                    "row_id": row_id,
                    "ledger_seq": seq,
                    "provisional": provisional,
                    "raw": args[6],
                }
            elif seq is not None and (
                existing["ledger_seq"] is None or existing["provisional"]
            ):
                existing["ledger_seq"] = seq
                existing["provisional"] = provisional
        elif head.startswith("UPDATE omninode_internal.work_ledger_rows") and (
            "SET ledger_seq = $2" in sql
        ):
            row_id, seq = args[0], args[1]
            if self._holder(seq, row_id) is not None:
                raise _UniqueViolationError(f"ledger_seq {seq} is held")
            self.rows[row_id]["ledger_seq"] = seq


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    @asynccontextmanager
    async def acquire(self) -> Any:
        yield self._connection


class _Db:
    dsn = ""

    def __init__(self, connection: _Connection) -> None:
        self.pool = _Pool(connection)


def _writer() -> tuple[WorkLedgerProjectionWriter, _Connection]:
    connection = _Connection()
    writer = WorkLedgerProjectionWriter()
    writer._db = cast("Any", _Db(connection))
    return writer, connection


async def _project(
    writer: WorkLedgerProjectionWriter, raw_row: str, seq: int | None
) -> None:
    # The envelope the emit node publishes, derived from its own row parser rather than typed by hand.
    envelope = parse_ledger_row(raw_row, source="onex-ledger").model_dump(mode="json")
    meta = MessageMeta(partition=0, offset=1, fallback_id="t", topic=TOPIC)
    await writer.project_event(TOPIC, {**envelope, "ledger_seq": seq}, meta)


def _seqs(connection: _Connection) -> dict[str, int]:
    return {row["raw"]: row["ledger_seq"] for row in connection.rows.values()}


@pytest.mark.asyncio
async def test_tip_plus_one_next_judged_row_still_projects_and_none_lands_in_conflicts() -> (
    None
):
    """The reviewer's reproduction: an unsequenced live row at tip+1, then the judge's next row."""
    writer, connection = _writer()
    await _project(writer, _claim(1), 1)  # the judge's row 1
    await _project(writer, _claim(2), None)  # live, no sequence: projected at tip+1 = 2
    assert _seqs(connection)[_claim(2)] == 2
    await _project(writer, _claim(3), 2)  # the judge's next row states 2
    assert connection.conflicts == {}
    seqs = _seqs(connection)
    assert seqs[_claim(3)] == 2  # the judge's number stands
    assert seqs[_claim(1)] == 1
    assert (
        seqs[_claim(2)] == 3
    )  # the projection-assigned row moved, and was not dropped


@pytest.mark.asyncio
async def test_next_judged_rows_after_two_unsequenced_rows_all_project_in_order() -> (
    None
):
    writer, connection = _writer()
    await _project(writer, _claim(1), None)  # provisional 1
    await _project(writer, _claim(2), None)  # provisional 2
    await _project(writer, _claim(3), 1)  # judge 1 displaces provisional 1
    await _project(writer, _claim(4), 2)  # judge 2 displaces provisional 2
    assert connection.conflicts == {}
    seqs = _seqs(connection)
    assert (seqs[_claim(3)], seqs[_claim(4)]) == (1, 2)
    assert sorted(seqs.values()) == [1, 2, 3, 4]  # gapless and each sequence held once


@pytest.mark.asyncio
async def test_next_judged_copy_of_the_row_settles_its_own_provisional_sequence() -> (
    None
):
    writer, connection = _writer()
    await _project(writer, _claim(1), None)  # provisional 1
    await _project(writer, _claim(1), 5)  # the judge accepted the same row as 5
    row = next(iter(connection.rows.values()))
    assert (row["ledger_seq"], row["provisional"]) == (5, False)
    assert connection.conflicts == {}


@pytest.mark.asyncio
async def test_a_judge_sequence_held_by_a_judged_row_is_still_a_conflict() -> None:
    writer, connection = _writer()
    await _project(writer, _claim(1), 1)
    await _project(writer, _claim(2), 1)
    assert list(connection.conflicts.values()) == [1]
    assert _seqs(connection) == {_claim(1): 1}


@pytest.mark.asyncio
async def test_a_redelivered_unsequenced_row_keeps_its_sequence_and_takes_no_second_one() -> (
    None
):
    writer, connection = _writer()
    await _project(writer, _claim(1), None)
    await _project(writer, _claim(1), None)
    assert _seqs(connection) == {_claim(1): 1}
    assert connection.conflicts == {}
