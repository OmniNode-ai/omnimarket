# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427: the reaper's pending-claims query reads a partial index.

The delegation reaper asks the claim store for every claim with no terminal on
each scan. The table had only its primary-key index, so that query read the
whole file. A store step adds a partial index over the pending rows; these tests
pin that every store gains it once, that a read-only store is still read, and
that the claim, terminal-slot and reap answers are the same with it as without.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegation_reap_context import (
    ModelDelegationReapContext,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_delegation_claim import (
    CLAIMS_TABLE,
    LATE_EVIDENCE_PREFIX,
    REAP_CONTEXT_PREFIX,
    TERMINAL_SLOT_PREFIX,
    DelegationClaimPort,
)
from omnimarket.projection import sqlite_database
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit

STEP = "omn17427_claims_pending_index"
INDEX = "delegate_skill_command_claims_pending_idx"
STEPS_TABLE = "omnimarket_sqlite_store_steps"
# The statement SqliteDatabaseAdapter.query builds for the reaper's filter.
PENDING_QUERY = f"SELECT * FROM {CLAIMS_TABLE} WHERE terminal_json = :terminal_json"


def _indexes(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as conn:
        return {
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ?",
                (CLAIMS_TABLE,),
            )
        }


def _step_rows(path: Path) -> int:
    with closing(sqlite3.connect(path)) as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {STEPS_TABLE} WHERE step = ?", (STEP,)
        ).fetchone()
        return int(row[0])


def _claim_rows(path: Path) -> int:
    with closing(sqlite3.connect(path)) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {CLAIMS_TABLE}").fetchone()[0])


def _plan(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        f"EXPLAIN QUERY PLAN {PENDING_QUERY}", {"terminal_json": ""}
    ).fetchall()
    return " | ".join(str(row[3]) for row in rows)


def _context(deadline_at: datetime) -> ModelDelegationReapContext:
    return ModelDelegationReapContext(
        correlation_id=uuid4(),
        task_type="test",
        tenant_id="omninode",
        ticket_id="OMN-17427",
        caller_lane="test",
        session_id=str(uuid4()),
        provenance=None,
        deadline_at=deadline_at,
    )


def _terminal(status: str) -> dict[str, object]:
    return {"data": {"status": status, "correlation_id": str(uuid4())}}


def _seed(path: Path, now: datetime) -> dict[str, set[UUID]]:
    """Overdue and future pending claims, settled claims, and a claim with no
    reap context, written through the real port."""
    port = DelegationClaimPort(SqliteDatabaseAdapter(path))
    seeded: dict[str, set[UUID]] = {
        "overdue": set(),
        "future": set(),
        "settled": set(),
        "no_context": set(),
    }
    for kind, deadline in (
        ("overdue", now - timedelta(minutes=5)),
        ("overdue", now - timedelta(minutes=1)),
        ("future", now + timedelta(minutes=5)),
        ("settled", now - timedelta(minutes=5)),
    ):
        delivery_id = uuid4()
        context = _context(deadline)
        assert port.claim(
            delivery_id=delivery_id,
            correlation_id=context.correlation_id,
            reap_context=context,
        ).won
        seeded[kind].add(delivery_id)
        if kind == "settled":
            assert port.record_terminal(
                delivery_id=delivery_id, terminal=_terminal("SUCCESS")
            ).won
            # A losing second attempt leaves a late: evidence row.
            assert not port.record_terminal(
                delivery_id=delivery_id, terminal=_terminal("FAILED")
            ).won
    delivery_id = uuid4()
    assert port.claim(delivery_id=delivery_id, correlation_id=uuid4()).won
    seeded["no_context"].add(delivery_id)
    return seeded


def _store_that_predates_the_step(path: Path) -> None:
    """A store written before the step: no index and no step record."""
    SqliteDatabaseAdapter(path)._connect().close()
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(f"DROP INDEX {INDEX}")
        conn.execute(f"DELETE FROM {STEPS_TABLE} WHERE step = ?", (STEP,))
        conn.commit()
    assert INDEX not in _indexes(path)


def _copy_without_index(source: Path, target: Path) -> None:
    with (
        closing(sqlite3.connect(source)) as src,
        closing(sqlite3.connect(target)) as dst,
    ):
        src.backup(dst)
        dst.execute(f"DROP INDEX {INDEX}")
        dst.commit()


@contextmanager
def _opened_read_only(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The adapter's connections open the file read-only. Mode bits cannot
    make a file read-only for root, so the connection is opened that way."""
    connect = sqlite3.connect

    def connect_read_only(database: Any, *args: Any, **kwargs: Any) -> Any:
        return connect(f"file:{database}?mode=ro", *args, **{**kwargs, "uri": True})

    monkeypatch.setattr(sqlite3, "connect", connect_read_only)
    try:
        yield
    finally:
        monkeypatch.undo()


@pytest.mark.parametrize("predates", [False, True], ids=["fresh", "predates-step"])
def test_every_store_gains_the_pending_index_once(
    tmp_path: Path, predates: bool
) -> None:
    path = tmp_path / "claims.sqlite"
    if predates:
        _store_that_predates_the_step(path)

    conn = SqliteDatabaseAdapter(path)._connect()
    try:
        plan = _plan(conn)
    finally:
        conn.close()

    assert INDEX in _indexes(path)
    assert _step_rows(path) == 1
    assert INDEX in plan, plan
    # A second open finds the step recorded and runs nothing.
    SqliteDatabaseAdapter(path)._connect().close()
    assert _step_rows(path) == 1


def test_a_read_only_store_that_predates_the_step_is_read_as_it_is(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "claims.sqlite"
    _seed(path, datetime.now(UTC))
    _store_that_predates_the_step(path)
    expected = _claim_rows(path)
    caplog.set_level(logging.WARNING, logger=sqlite_database.__name__)

    with _opened_read_only(monkeypatch):
        rows = SqliteDatabaseAdapter(path).query(CLAIMS_TABLE)

    assert len(rows) == expected
    assert INDEX not in _indexes(path)
    warnings = [
        record
        for record in caplog.records
        if record.name == sqlite_database.__name__ and record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert str(path) in message
    assert "read-only" in message
    assert "indexed the pending delegate-skill command claims" in message


def test_the_step_deletes_no_row(tmp_path: Path) -> None:
    path = tmp_path / "claims.sqlite"
    _seed(path, datetime.now(UTC))
    _store_that_predates_the_step(path)
    before = _claim_rows(path)
    assert before > 0

    SqliteDatabaseAdapter(path)._connect().close()

    assert INDEX in _indexes(path)
    assert _claim_rows(path) == before


def test_claim_terminal_and_reap_answers_are_unchanged_by_the_index(
    tmp_path: Path,
) -> None:
    path = tmp_path / "claims.sqlite"
    now = datetime.now(UTC)
    seeded = _seed(path, now)
    assert INDEX in _indexes(path)
    port = DelegationClaimPort(SqliteDatabaseAdapter(path))

    # A redelivery of a settled command loses and is served its terminal.
    settled = next(iter(seeded["settled"]))
    again = port.claim(delivery_id=settled, correlation_id=uuid4())
    assert again.won is False
    assert again.served_terminal is not None
    served_data = again.served_terminal["data"]
    assert isinstance(served_data, dict)
    assert served_data["status"] == "SUCCESS"

    # A redelivery while the first attempt is in flight loses with nothing served.
    in_flight = next(iter(seeded["future"]))
    assert port.claim(delivery_id=in_flight, correlation_id=uuid4()).won is False

    # Exactly one of two terminals for one delivery wins.
    racing = uuid4()
    assert port.claim(delivery_id=racing, correlation_id=uuid4()).won
    first = port.record_terminal(delivery_id=racing, terminal=_terminal("SUCCESS"))
    second = port.record_terminal(delivery_id=racing, terminal=_terminal("FAILED"))
    assert [first.won, second.won].count(True) == 1

    stalled = port.stalled_claims(now=now, limit=100)
    stalled_ids = {claim.delivery_id for claim in stalled}
    assert stalled_ids == seeded["overdue"]

    with closing(sqlite3.connect(path)) as conn:
        keys = {
            str(r[0]) for r in conn.execute(f"SELECT delivery_id FROM {CLAIMS_TABLE}")
        }
    for prefix in (REAP_CONTEXT_PREFIX, TERMINAL_SLOT_PREFIX, LATE_EVIDENCE_PREFIX):
        assert any(key.startswith(prefix) for key in keys), prefix

    # The same store with the index dropped answers the same set.
    copy = tmp_path / "claims_without_index.sqlite"
    _copy_without_index(path, copy)
    unindexed = SqliteDatabaseAdapter(copy)
    conn = unindexed._connect()
    try:
        assert INDEX not in _plan(conn)
    finally:
        conn.close()
    assert INDEX not in _indexes(copy)
    unindexed_ids = {
        claim.delivery_id
        for claim in DelegationClaimPort(unindexed).stalled_claims(now=now, limit=100)
    }
    assert unindexed_ids == stalled_ids
    assert sorted(
        tuple(sorted(row.items()))
        for row in SqliteDatabaseAdapter(copy).query(
            CLAIMS_TABLE, {"terminal_json": ""}
        )
    ) == sorted(
        tuple(sorted(row.items()))
        for row in SqliteDatabaseAdapter(path).query(
            CLAIMS_TABLE, {"terminal_json": ""}
        )
    )


CORRELATION_STEP = "omn17427_claims_correlation_index"
CORRELATION_INDEX = "delegate_skill_command_claims_correlation_idx"
# The statement SqliteDatabaseAdapter.query builds for pending_claims.
CORRELATION_QUERY = (
    f"SELECT * FROM {CLAIMS_TABLE} WHERE correlation_id = :correlation_id"
)


def _correlation_step_rows(path: Path) -> int:
    with closing(sqlite3.connect(path)) as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {STEPS_TABLE} WHERE step = ?", (CORRELATION_STEP,)
        ).fetchone()
        return int(row[0])


def _correlation_plan(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        f"EXPLAIN QUERY PLAN {CORRELATION_QUERY}", {"correlation_id": str(uuid4())}
    ).fetchall()
    return " | ".join(str(row[3]) for row in rows)


@pytest.mark.parametrize("predates", [False, True], ids=["fresh", "predates-step"])
def test_every_store_gains_the_correlation_index_once(
    tmp_path: Path, predates: bool
) -> None:
    path = tmp_path / "claims.sqlite"
    if predates:
        SqliteDatabaseAdapter(path)._connect().close()
        with closing(sqlite3.connect(path)) as conn:
            conn.execute(f"DROP INDEX IF EXISTS {CORRELATION_INDEX}")
            conn.execute(
                f"DELETE FROM {STEPS_TABLE} WHERE step = ?", (CORRELATION_STEP,)
            )
            conn.commit()
        assert CORRELATION_INDEX not in _indexes(path)

    conn = SqliteDatabaseAdapter(path)._connect()
    try:
        plan = _correlation_plan(conn)
    finally:
        conn.close()

    assert CORRELATION_INDEX in _indexes(path)
    assert _correlation_step_rows(path) == 1
    assert CORRELATION_INDEX in plan, plan
    # A second open finds the step recorded and runs nothing.
    SqliteDatabaseAdapter(path)._connect().close()
    assert _correlation_step_rows(path) == 1


def _pending_answer(
    port: DelegationClaimPort, correlation_id: UUID
) -> list[tuple[UUID, datetime, str]]:
    return [
        (claim.delivery_id, claim.claimed_at, claim.context.model_dump_json())
        for claim in port.pending_claims(correlation_id=correlation_id)
    ]


def test_pending_claims_answers_are_unchanged_by_the_correlation_index(
    tmp_path: Path,
) -> None:
    path = tmp_path / "claims.sqlite"
    now = datetime.now(UTC)
    _seed(path, now)
    port = DelegationClaimPort(SqliteDatabaseAdapter(path))

    # One correlation with exactly one pending delivery.
    single_delivery = uuid4()
    single_context = _context(now + timedelta(minutes=5))
    assert port.claim(
        delivery_id=single_delivery,
        correlation_id=single_context.correlation_id,
        reap_context=single_context,
    ).won
    # One correlation reused by two deliveries.
    reused_context = _context(now + timedelta(minutes=5))
    for _ in range(2):
        assert port.claim(
            delivery_id=uuid4(),
            correlation_id=reused_context.correlation_id,
            reap_context=reused_context,
        ).won
    assert CORRELATION_INDEX in _indexes(path)

    single = _pending_answer(port, single_context.correlation_id)
    reused = _pending_answer(port, reused_context.correlation_id)
    assert [delivery for delivery, _, _ in single] == [single_delivery]
    assert reused == []

    # The same store with the index dropped answers the same claims.
    copy = tmp_path / "claims_without_correlation_index.sqlite"
    with (
        closing(sqlite3.connect(path)) as src,
        closing(sqlite3.connect(copy)) as dst,
    ):
        src.backup(dst)
        dst.execute(f"DROP INDEX {CORRELATION_INDEX}")
        dst.commit()
    unindexed = SqliteDatabaseAdapter(copy)
    conn = unindexed._connect()
    try:
        assert CORRELATION_INDEX not in _correlation_plan(conn)
    finally:
        conn.close()
    assert CORRELATION_INDEX not in _indexes(copy)
    unindexed_port = DelegationClaimPort(unindexed)
    assert _pending_answer(unindexed_port, single_context.correlation_id) == single
    assert _pending_answer(unindexed_port, reused_context.correlation_id) == reused
