# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17183: real-Postgres write-path proof for ``session_replay_snapshots``.

## The defect this file locks

``handle()`` called ``project(event, db, topic)`` with no reducer state, and
``project()`` reset to a fresh ``ModelSessionReplayState`` on every message. The
row key was ``sha256(f"{session_id}::{sequence}")`` with a ``sequence`` that
never advanced, so every event of a session derived the SAME ``snapshot_id`` and
each UPSERT overwrote the previous one. Live on the .201 stability lane
2026-08-30: **69,014 consumed ``tool-executed`` events materialized 15 rows**,
one per session, ``cumulative_tokens`` stuck at 0, consumer lag 0 and DLQ 0
throughout. Nothing errored.

## Why the in-memory double cannot prove the fix

``InmemoryDatabaseAdapter`` accepts any Python object for any column and has no
constraints at all. Three things about this table exist only in real Postgres:

* ``timestamp`` is ``TIMESTAMPTZ`` while the handler produces an ISO-8601
  **string** -- the str-vs-column-type class of OMN-15905. It works because
  ``PostgresSyncProjectionAdapter._adapt`` passes the str through and psycopg2
  binds it as an untyped literal Postgres casts server-side. That is a fact
  about the real driver, not about the double.
* ``state_delta`` is ``JSONB`` while the handler produces a **dict**, adapted
  through ``psycopg2.extras.Json``.
* ``UNIQUE (session_id, sequence)`` is a real constraint. The collapse defect
  never tripped it (every row reused sequence 0 on the same ``snapshot_id``), but
  the FIX advances ``sequence``, so a rehydration that mis-computed the next
  ordinal would raise ``UniqueViolation`` here and nowhere else.

This suite therefore writes through the **production**
``PostgresSyncProjectionAdapter``, not a hand-rolled double, and drives
``handle()`` -- the method ``handler_wiring._invoke_projection`` actually calls.

## Skip contract

SKIPS (never ERRORs) without a reachable database or an applied relation,
mirroring ``tests/test_omn16180_real_postgres_work_events_write_path.py``.
Every row it writes is scoped to a synthetic ``omn17183-`` session id and
deleted on the way out.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any
from urllib.parse import quote_plus

import pytest

from omnimarket.nodes.node_projection_session_replay.handlers.handler_projection_session_replay import (
    TABLE,
    TOPIC_PROMPT_SUBMITTED,
    TOPIC_SESSION_ENDED,
    TOPIC_SESSION_OUTCOME,
    TOPIC_SESSION_STARTED,
    TOPIC_TOOL_EXECUTED,
    HandlerProjectionSessionReplay,
)
from omnimarket.projection.postgres_sync_database import PostgresSyncProjectionAdapter

_QUALIFIED = f"public.{TABLE}"
_SESSION_PREFIX = "omn17183-real-pg"

# One session lifecycle: six events, three of them token-bearing.
_LIFECYCLE: list[tuple[str, dict[str, object]]] = [
    (TOPIC_SESSION_STARTED, {"timestamp": "2026-08-30T09:00:00+00:00"}),
    (
        TOPIC_PROMPT_SUBMITTED,
        {
            "timestamp": "2026-08-30T09:00:01+00:00",
            "prompt_preview": "summarize the lane",
            "prompt_length": 18,
            "tokens_used": 12,
        },
    ),
    (
        TOPIC_TOOL_EXECUTED,
        {
            "timestamp": "2026-08-30T09:00:02+00:00",
            "tool_name": "Read",
            "tool_input": {"path": "README.md"},
            "tokens_used": 8,
        },
    ),
    (
        TOPIC_TOOL_EXECUTED,
        {
            "timestamp": "2026-08-30T09:00:03+00:00",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "tokens_used": 5,
        },
    ),
    (
        TOPIC_SESSION_OUTCOME,
        {"timestamp": "2026-08-30T09:00:04+00:00", "outcome": "success"},
    ),
    (TOPIC_SESSION_ENDED, {"timestamp": "2026-08-30T09:00:05+00:00"}),
]
_EXPECTED_CUMULATIVE = [0, 12, 20, 25, 25, 25]


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
    """Open a psycopg2 connection, or skip. Never fail on absent infrastructure."""
    if not os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    ):
        pytest.skip(
            "POSTGRES_PASSWORD not set -- skipping OMN-17183 real-Postgres "
            "session_replay write-path proof"
        )
    psycopg2 = pytest.importorskip("psycopg2")
    try:
        conn = psycopg2.connect(_dsn())  # no-contract-check: test harness probe
    except psycopg2.Error as exc:  # pragma: no cover - infrastructure dependent
        pytest.skip(f"no reachable Postgres for OMN-17183 write path: {exc}")
    conn.autocommit = True
    return conn


def _ensure_table_or_skip(conn: Any) -> None:
    """Skip -- never fail -- when the relation has not been applied here.

    Called OUTSIDE any ``try`` whose ``finally`` deletes from the table:
    ``pytest.skip`` raises, and a cleanup ``DELETE`` against a missing relation
    would replace the skip with an ``UndefinedTable`` failure.
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
        cur.execute(
            f"DELETE FROM {_QUALIFIED} WHERE session_id = %s",
            (session_id,),
        )


def _rows(conn: Any, session_id: str) -> list[tuple[Any, ...]]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT snapshot_id, sequence, timestamp, event_type, node_name, "
            f"state_delta, cumulative_tokens, is_checkpoint FROM {_QUALIFIED} "
            "WHERE session_id = %s ORDER BY sequence ASC",
            (session_id,),
        )
        return list(cur.fetchall())


def _dispatch_lifecycle(session_id: str) -> None:
    """Drive handle() through the PRODUCTION sync adapter, once per event.

    This is the runtime's own shape: the event payload plus the injected
    ``_db``/``_topic`` bookkeeping keys, handed to ``handle()``.
    """
    handler = HandlerProjectionSessionReplay()
    adapter = PostgresSyncProjectionAdapter(_dsn())
    for topic, payload in _LIFECYCLE:
        input_data: dict[str, object] = {
            "session_id": session_id,
            **payload,
            "_db": adapter,
            "_topic": topic,
        }
        result = handler.handle(input_data)
        assert result["rows_upserted"] == 1, result


@pytest.mark.integration
def test_real_column_types_accept_the_handler_row_and_round_trip() -> None:
    """TIMESTAMPTZ, JSONB and INT all accept what the handler hands the adapter."""
    conn = _connect_or_skip()
    session_id = f"{_SESSION_PREFIX}-roundtrip"
    try:
        _ensure_table_or_skip(conn)
        try:
            _purge(conn, session_id)

            handler = HandlerProjectionSessionReplay()
            adapter = PostgresSyncProjectionAdapter(_dsn())
            result = handler.handle(
                {
                    "session_id": session_id,
                    "timestamp": "2026-08-30T09:00:02+00:00",
                    "tool_name": "Read",
                    "tool_input": {"path": "README.md"},
                    "tokens_used": 8,
                    "_db": adapter,
                    "_topic": TOPIC_TOOL_EXECUTED,
                }
            )
            assert result["rows_upserted"] == 1

            rows = _rows(conn, session_id)
            assert len(rows) == 1, "the real write path produced no row"
            (
                snapshot_id,
                sequence,
                timestamp,
                event_type,
                node_name,
                state_delta,
                cumulative,
                is_checkpoint,
            ) = rows[0]

            # TIMESTAMPTZ parsed the handler's ISO STRING (psycopg2 binds it as
            # an untyped literal; Postgres casts server-side) and kept the instant.
            assert isinstance(timestamp, datetime)
            assert timestamp == datetime.fromisoformat("2026-08-30T09:00:02+00:00")
            # JSONB parsed the handler's dict, adapted via psycopg2.extras.Json.
            stored_delta = (
                state_delta
                if isinstance(state_delta, dict)
                else json.loads(state_delta)
            )
            assert stored_delta["tool_name"] == "Read"
            assert stored_delta["tool_input"] == {"path": "README.md"}
            assert event_type == "tool_call"
            assert node_name == "Read"
            assert sequence == 0
            assert cumulative == 8
            assert is_checkpoint is False
            # UUID-shaped, as the dashboard expects.
            assert [len(part) for part in str(snapshot_id).split("-")] == [
                8,
                4,
                4,
                4,
                12,
            ]
        finally:
            _purge(conn, session_id)
    finally:
        conn.close()


@pytest.mark.integration
def test_a_session_of_events_does_not_collapse_onto_one_row() -> None:
    """THE OMN-17183 regression, on real Postgres.

    RED before the fix: six ``handle()`` calls left ONE row at sequence 0 with
    ``cumulative_tokens = 0`` -- the 69,014 -> 15 collapse, reproduced exactly.
    GREEN after: six rows, six distinct snapshot_ids, a monotonic ``sequence``
    that satisfies ``UNIQUE (session_id, sequence)``, and a real running total.
    """
    conn = _connect_or_skip()
    session_id = f"{_SESSION_PREFIX}-no-collapse"
    try:
        _ensure_table_or_skip(conn)
        try:
            _purge(conn, session_id)
            _dispatch_lifecycle(session_id)

            rows = _rows(conn, session_id)
            assert len(rows) == len(_LIFECYCLE), (
                f"{len(_LIFECYCLE)} events materialized {len(rows)} row(s) -- "
                "the session collapsed"
            )
            assert [row[1] for row in rows] == list(range(len(_LIFECYCLE)))
            assert len({str(row[0]) for row in rows}) == len(_LIFECYCLE)
            assert [row[6] for row in rows] == _EXPECTED_CUMULATIVE
            assert [row[3] for row in rows] == [
                "session_start",
                "user_input",
                "tool_call",
                "tool_call",
                "checkpoint",
                "session_end",
            ]
        finally:
            _purge(conn, session_id)
    finally:
        conn.close()


@pytest.mark.integration
def test_redelivery_is_idempotent_on_the_real_on_conflict_path() -> None:
    """Replaying the whole stream adds no rows and double-counts no tokens.

    Idempotency here is the real ``ON CONFLICT (snapshot_id) DO UPDATE``, not
    the in-memory double's merge semantics. It holds because row identity is the
    event's content address, not a counter the reducer has to thread correctly.
    """
    conn = _connect_or_skip()
    session_id = f"{_SESSION_PREFIX}-redelivery"
    try:
        _ensure_table_or_skip(conn)
        try:
            _purge(conn, session_id)
            _dispatch_lifecycle(session_id)
            first_pass = _rows(conn, session_id)

            _dispatch_lifecycle(session_id)  # at-least-once redelivery
            second_pass = _rows(conn, session_id)

            assert len(second_pass) == len(_LIFECYCLE)
            assert [row[6] for row in second_pass] == _EXPECTED_CUMULATIVE
            assert [str(row[0]) for row in second_pass] == [
                str(row[0]) for row in first_pass
            ]
        finally:
            _purge(conn, session_id)
    finally:
        conn.close()
