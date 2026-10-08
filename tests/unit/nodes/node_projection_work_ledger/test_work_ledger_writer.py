# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The projection writer is the entry the runtime calls, and it writes (OMN-19513, rule 7a)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.runtime.runtime_local_adapter import _invoke_handle_method

from omnimarket.events.enum_ledger_row_type import (
    EnumLedgerRowType,
)
from omnimarket.nodes.node_projection_work_ledger.contract_topics import (
    ALL_SUBSCRIBE_TOPICS,
    SUBSCRIBE_TOPICS,
)
from omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_projection import (
    HandlerProjectionWorkLedger,
    WorkLedgerProjectionWriter,
)

pytestmark = pytest.mark.unit

_NODE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_projection_work_ledger"
)
CLAIM = "2026-09-28T10:00:00Z | CLAIM | lane=alpha | ticket=OMN-1 | est ~1 lane-hours; displaces x; (OMN-1) | work"
TERM = (
    "2026-09-28T10:30:00Z | TERMINAL | lane=alpha | ticket=OMN-1 | friction=none | done"
)


class _FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeConnection:
    def __init__(self, db: _FakeDb) -> None:
        self._db = db

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction()

    async def execute(self, sql: str, *args: Any) -> None:
        if "pg_advisory_xact_lock" in sql:
            self._db.locks.append(args)
            return
        await self._db.execute(sql, *args)


class _FakeAcquire:
    def __init__(self, db: _FakeDb) -> None:
        self._db = db

    async def __aenter__(self) -> _FakeConnection:
        return _FakeConnection(self._db)

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakePool:
    def __init__(self, db: _FakeDb) -> None:
        self._db = db

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._db)


class _FakeDb:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.locks: list[tuple[Any, ...]] = []
        self.connected = 0
        self.closed = 0

    async def connect(self) -> None:
        self.connected += 1

    async def close(self) -> None:
        self.closed += 1

    async def execute(self, sql: str, *args: Any) -> None:
        self.statements.append((sql, args))

    @property
    def pool(self) -> _FakePool:
        return _FakePool(self)


def _writer(db: _FakeDb) -> WorkLedgerProjectionWriter:
    writer = WorkLedgerProjectionWriter()
    writer._db = db  # type: ignore[assignment]
    return writer


def test_the_writer_declares_in_process_dispatch() -> None:
    assert WorkLedgerProjectionWriter.onex_runtime_inprocess_dispatch is True


def test_the_writer_subscribes_to_the_eleven_row_topics_the_contract_declares() -> None:
    assert sorted(_writer(_FakeDb()).subscribe_topics) == sorted(
        topic for t in EnumLedgerRowType for topic in (t.topic, t.typed_topic)
    )
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text())
    assert sorted(contract["event_bus"]["subscribe_topics"]) == sorted(
        ALL_SUBSCRIBE_TOPICS
    )
    assert len(SUBSCRIBE_TOPICS) == 11


def test_a_claim_message_writes_the_log_row_and_opens_the_entity() -> None:
    db = _FakeDb()
    result = _writer(db).handle(
        {"raw_row": CLAIM, "_topic": EnumLedgerRowType.CLAIM.topic}
    )
    assert result == {"rows_upserted": 2}
    assert (db.connected, db.closed) == (1, 1)
    sqls = [s for s, _ in db.statements]
    assert "work_ledger_rows" in sqls[0]
    assert "DO UPDATE SET ledger_seq = EXCLUDED.ledger_seq" in sqls[0]
    assert "work_ledger_state" in sqls[1]
    assert "opened_at" in sqls[1]
    assert db.statements[1][1][0] == "claim:alpha"
    # The legacy arrival takes the same ledger lock the typed path holds, so it
    # cannot interleave with typed reconciliation.
    assert db.locks == [("work-ledger:rolling-work-ledger",)]


def test_a_terminal_message_writes_only_the_closing_column_group() -> None:
    db = _FakeDb()
    _writer(db).handle({"raw_row": TERM, "_topic": EnumLedgerRowType.TERMINAL.topic})
    close_sql, close_args = db.statements[1]
    assert "closed_at = EXCLUDED.closed_at" in close_sql
    assert "opened_at" not in close_sql
    assert close_args[0] == "claim:alpha"


def test_the_state_upserts_are_guarded_on_time_and_row_id() -> None:
    db = _FakeDb()
    w = _writer(db)
    w.handle({"raw_row": CLAIM, "_topic": EnumLedgerRowType.CLAIM.topic})
    w.handle({"raw_row": TERM, "_topic": EnumLedgerRowType.TERMINAL.topic})
    for sql, _ in db.statements:
        if "work_ledger_state" in sql:
            assert "<= (EXCLUDED." in sql


def test_an_unsubscribed_topic_is_refused() -> None:
    with pytest.raises(Exception, match="unsubscribed"):
        _writer(_FakeDb()).handle(
            {"raw_row": CLAIM, "_topic": "onex.evt.other.thing.v1"}
        )


def test_the_pure_handler_is_reachable_through_the_runtime_adapters_own_helper() -> (
    None
):
    """The adapter builds the request from the unwrapped bus payload, extras and all."""
    payload = {
        "ledger_id": "rolling-work-ledger",
        "row_id": "0" * 64,
        "row_timestamp": "2026-09-28T10:00:00Z",
        "raw_row": CLAIM,
        "emitted_at": "2026-09-28T10:00:01+00:00",
        "entity_id": "abc",
        "lane": "clobbered-by-the-hook-journal",
        "schema_version": "1.0.0",
    }
    result = _invoke_handle_method(HandlerProjectionWorkLedger().handle, payload)
    assert result.ops[0].entity_key == "claim:alpha"


@pytest.mark.parametrize("ledger_seq", [None, 17])
def test_ledger_seq_writer_binds_and_only_fills_null(ledger_seq: int | None) -> None:
    db = _FakeDb()
    _writer(db).handle(
        {
            "raw_row": CLAIM,
            "_topic": EnumLedgerRowType.CLAIM.topic,
            "ledger_seq": ledger_seq,
        }
    )
    sql, args = db.statements[0]
    assert "projected_at, ledger_seq)" in sql
    assert "$10)" in sql
    assert "ON CONFLICT (row_id) DO UPDATE SET ledger_seq = EXCLUDED.ledger_seq" in sql
    assert (
        "WHERE omninode_internal.work_ledger_rows.ledger_seq IS NULL "
        "AND EXCLUDED.ledger_seq IS NOT NULL"
    ) in sql
    assert len(args) == 10
    assert args[9] == ledger_seq
    assert args[6] == CLAIM
    assert db.locks == [("work-ledger:rolling-work-ledger",)]


def test_ledger_seq_migration_is_idempotent_and_nonunique() -> None:
    sql = (_NODE / "migrations/0003_work_ledger_seq.sql").read_text()
    ddl = "\n".join(line for line in sql.splitlines() if not line.startswith("--"))
    assert "ADD COLUMN IF NOT EXISTS ledger_seq BIGINT" in ddl
    assert "CREATE INDEX IF NOT EXISTS idx_work_ledger_rows_seq" in ddl
    assert "(ledger_id, ledger_seq)" in ddl
    assert "WHERE ledger_seq IS NOT NULL" in ddl
    assert "UNIQUE" not in ddl.upper()
