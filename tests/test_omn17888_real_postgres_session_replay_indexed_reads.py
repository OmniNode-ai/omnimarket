# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17888: real-Postgres proof that the session-replay reads are bounded.

## The defect this file locks

``HandlerProjectionSessionReplay.project`` opened with

    session_rows = db.query(TABLE, {"session_id": event.session_id})

and scanned that list twice in Python -- once for the row matching the
``snapshot_id`` about to be written, once for the maximum ``sequence``. The
whole session was read back on EVERY event, so projecting a session cost
O(n^2) in its length. On the .201 dev lane at 2026-09-07T15:53Z the busiest
``session_id`` held 100,441 of ``public.session_replay_snapshots``' 103,468 rows
and grew ~3,029 rows/hour; each of its events materialised ~100k rows into the
runtime process. The OMN-17888 first pass bounded the runtime read seam at
125,000 rows, which for that session turned the OOM into a scheduled refusal
around 2026-09-08T00:00Z.

## Why the in-memory double cannot prove this half

``InmemoryDatabaseAdapter`` sorts a Python list. Three things live only here:

* ``ORDER BY sequence DESC LIMIT 1`` is emitted as SQL and answered by
  ``idx_session_replay_session_sequence btree (session_id, sequence)``. Whether
  the statement is even syntactically valid against the real relation is a fact
  about the adapter's SQL construction, not about the double.
* ``sequence`` is ``INTEGER`` in Postgres and an arbitrary Python object in the
  double, so "highest ordinal" is decided by two different comparison rules.
  A stringly-typed ordering would put 9 after 100 in one of them.
* ``UNIQUE (session_id, sequence)`` is a real constraint. The whole point of
  continuing from the stored MAXIMUM rather than a row count is that an
  out-of-band delete leaves a gap; get it wrong and this raises
  ``UniqueViolation`` here and nowhere else.

Writes go through the production ``PostgresSyncProjectionAdapter`` and
``handle()`` -- the method the runtime actually calls.

## Skip contract

SKIPS (never ERRORs) without a reachable database or an applied relation,
mirroring ``tests/test_omn17183_real_postgres_session_replay_write_path.py``.
Every row is scoped to a synthetic ``omn17888-`` session id and deleted on the
way out.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote_plus

import pytest

from omnimarket.nodes.node_projection_session_replay.handlers.handler_projection_session_replay import (
    CONFLICT_KEY,
    TABLE,
    TOPIC_TOOL_EXECUTED,
    HandlerProjectionSessionReplay,
)
from omnimarket.projection.postgres_sync_database import PostgresSyncProjectionAdapter

_QUALIFIED = f"public.{TABLE}"
_SESSION_PREFIX = "omn17888-real-pg"


def _dsn() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnidash_analytics")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


def _connect_or_skip() -> Any:
    if not os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    ):
        pytest.skip(
            "POSTGRES_PASSWORD not set -- skipping OMN-17888 real-Postgres "
            "indexed-read proof"
        )
    psycopg2 = pytest.importorskip("psycopg2")
    try:
        conn = psycopg2.connect(_dsn())  # no-contract-check: test harness probe
    except psycopg2.Error as exc:  # pragma: no cover - infrastructure dependent
        pytest.skip(f"no reachable Postgres for OMN-17888 indexed reads: {exc}")
        # Unreachable: `pytest.skip` raises. Stated explicitly so the except
        # branch cannot fall through -- static flow analysis does not know that
        # `skip` never returns, and `py/uninitialized-local-variable` fires on
        # the `conn` use below without it.
        raise
    conn.autocommit = True
    return conn


def _ensure_table_or_skip(conn: Any) -> None:
    """Skip -- never fail -- when the relation has not been applied here.

    Called OUTSIDE any ``try`` whose ``finally`` deletes from the table: a
    cleanup ``DELETE`` against a missing relation would replace the skip with
    an ``UndefinedTable`` failure.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (_QUALIFIED,))
        exists = cur.fetchone()[0]
    if not exists:
        pytest.skip(
            f"{_QUALIFIED} not present -- apply node_projection_session_replay/"
            "0001_create_session_replay_snapshots.sql first"
        )


def _purge(conn: Any, session_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {_QUALIFIED} WHERE session_id = %s", (session_id,))


def _project(session_id: str, index: int) -> dict[str, object]:
    """Drive ``handle()`` once, through the production sync adapter."""
    handler = HandlerProjectionSessionReplay()
    adapter = PostgresSyncProjectionAdapter(_dsn())
    return handler.handle(
        {
            "session_id": session_id,
            "emitted_at": "2026-09-07T15:53:00+00:00",
            "tool_name": "Read",
            "tool_input": {"path": f"file-{index}.md"},
            "tokens_used": 3,
            "_db": adapter,
            "_topic": TOPIC_TOOL_EXECUTED,
        }
    )


@pytest.mark.integration
def test_latest_row_read_returns_the_maximum_ordinal_not_the_first_row() -> None:
    """``ORDER BY sequence DESC LIMIT 1`` against the real relation.

    Six events give ordinals 0..5. The bounded read must return ordinal 5 and
    exactly one row -- the two properties reducer-state rehydration depends on.
    A statement that returned the FIRST row would still be one row, and would
    reset every session's ordinal to 1 on its next event, so the ordinal is
    asserted, not just the count.
    """
    conn = _connect_or_skip()
    session_id = f"{_SESSION_PREFIX}-latest"
    try:
        _ensure_table_or_skip(conn)
        try:
            _purge(conn, session_id)
            for index in range(6):
                assert _project(session_id, index)["rows_upserted"] == 1

            adapter = PostgresSyncProjectionAdapter(_dsn())
            latest = adapter.query(
                TABLE,
                {"session_id": session_id},
                order_by="sequence",
                descending=True,
                limit=1,
            )
            assert len(latest) == 1, f"bounded read returned {len(latest)} rows"
            assert latest[0]["sequence"] == 5
            assert latest[0]["cumulative_tokens"] == 18  # 6 events * 3 tokens

            # Control: the same read ascending returns the OTHER end, which is
            # what proves the ordering is real SQL and not an artefact of
            # insertion order.
            earliest = adapter.query(
                TABLE, {"session_id": session_id}, order_by="sequence", limit=1
            )
            assert earliest[0]["sequence"] == 0
        finally:
            _purge(conn, session_id)
    finally:
        conn.close()


@pytest.mark.integration
def test_ordinals_are_compared_as_integers_not_as_text() -> None:
    """9 must sort below 100, which is only true if ``sequence`` is INTEGER.

    Eleven events give ordinals 0..10. Under text ordering the maximum would be
    '9'; under integer ordering it is 10. The double cannot distinguish these
    because it holds Python ints either way.
    """
    conn = _connect_or_skip()
    session_id = f"{_SESSION_PREFIX}-int-order"
    try:
        _ensure_table_or_skip(conn)
        try:
            _purge(conn, session_id)
            for index in range(11):
                assert _project(session_id, index)["rows_upserted"] == 1

            adapter = PostgresSyncProjectionAdapter(_dsn())
            latest = adapter.query(
                TABLE,
                {"session_id": session_id},
                order_by="sequence",
                descending=True,
                limit=1,
            )
            assert latest[0]["sequence"] == 10, (
                "the maximum ordinal came back as 9, so `sequence` is being "
                "ordered as text"
            )
        finally:
            _purge(conn, session_id)
    finally:
        conn.close()


@pytest.mark.integration
def test_an_ordinal_gap_does_not_re_issue_an_ordinal_the_unique_holds() -> None:
    """The stored MAXIMUM, not a row count -- enforced here by the constraint.

    ``UNIQUE (session_id, sequence)`` is the reason the distinction matters. Six
    rows are written, the middle three deleted out of band (3 rows remain,
    maximum ordinal 5), and one more event projected. A count-based rehydration
    would emit ordinal 3, which the constraint still holds, and the write would
    raise ``UniqueViolation`` -- here and nowhere else.
    """
    conn = _connect_or_skip()
    session_id = f"{_SESSION_PREFIX}-gap"
    try:
        _ensure_table_or_skip(conn)
        try:
            _purge(conn, session_id)
            for index in range(6):
                assert _project(session_id, index)["rows_upserted"] == 1
            with conn.cursor() as cur:
                cur.execute(
                    f"DELETE FROM {_QUALIFIED} WHERE session_id = %s "
                    "AND sequence BETWEEN 1 AND 3",
                    (session_id,),
                )

            assert _project(session_id, 99)["rows_upserted"] == 1

            adapter = PostgresSyncProjectionAdapter(_dsn())
            latest = adapter.query(
                TABLE,
                {"session_id": session_id},
                order_by="sequence",
                descending=True,
                limit=1,
            )
            assert latest[0]["sequence"] == 6, (
                "the new ordinal came from a row count rather than the stored maximum"
            )
        finally:
            _purge(conn, session_id)
    finally:
        conn.close()


@pytest.mark.integration
def test_the_idempotency_lookup_is_an_equality_on_the_primary_key() -> None:
    """One row back for the key, and a redelivery does not append.

    ``snapshot_id`` is the table's PRIMARY KEY, so this is the cheapest read the
    relation can answer. It replaces scanning the session for the same row.
    """
    conn = _connect_or_skip()
    session_id = f"{_SESSION_PREFIX}-pk"
    try:
        _ensure_table_or_skip(conn)
        try:
            _purge(conn, session_id)
            assert _project(session_id, 0)["rows_upserted"] == 1

            adapter = PostgresSyncProjectionAdapter(_dsn())
            rows = adapter.query(TABLE, {"session_id": session_id})
            assert len(rows) == 1
            snapshot_id = rows[0][CONFLICT_KEY]

            by_key = adapter.query(TABLE, {CONFLICT_KEY: snapshot_id}, limit=1)
            assert len(by_key) == 1
            assert by_key[0]["session_id"] == session_id

            # Redelivery of the identical event re-writes the same row.
            assert _project(session_id, 0)["rows_upserted"] == 1
            assert len(adapter.query(TABLE, {"session_id": session_id})) == 1
        finally:
            _purge(conn, session_id)
    finally:
        conn.close()


@pytest.mark.integration
def test_the_real_adapter_refuses_the_malformed_bounded_reads() -> None:
    """The refusals reach the real adapter, before any statement is issued.

    ``descending`` with no ordering column would silently return an arbitrary
    row; a non-positive ``limit`` is not a question the caller meant to ask.
    Both are refused by the double as well -- asserted here so the two cannot
    diverge and make the double's version vacuous.
    """
    conn = _connect_or_skip()
    try:
        _ensure_table_or_skip(conn)
        adapter = PostgresSyncProjectionAdapter(_dsn())
        with pytest.raises(ValueError, match="descending requires an order_by column"):
            adapter.query(TABLE, {"session_id": "unused"}, descending=True)
        with pytest.raises(ValueError, match="limit must be a positive int"):
            adapter.query(TABLE, {"session_id": "unused"}, limit=0)
        with pytest.raises(ValueError, match="invalid order-column identifier"):
            adapter.query(TABLE, {"session_id": "unused"}, order_by="sequence; DROP")
    finally:
        conn.close()
