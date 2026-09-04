# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17774: real-Postgres proof that the published delta IS the stored row.

## What this locks that the in-memory suite cannot

``tests/unit/projection/test_omn17774_session_replay_bus_backed_chain.py`` proves
the chain against ``InmemoryDatabaseAdapter``, which accepts any Python object
for any column and has no constraints. Three facts about this write path exist
only against real Postgres, and all three land in the SNAPSHOT DELTA, not just
in the table:

* ``timestamp`` is ``TIMESTAMPTZ`` while the handler hands the adapter an
  ISO-8601 **string**. ``encode_snapshot_delta`` serializes whatever the handler
  wrote, so a str-vs-column-type mismatch (the OMN-15905 class) would put a
  different value on the bus than the one the database stored -- and the
  projection API serves the bus, so the page would render the wrong value with
  the right row present. Only a real driver settles what that value is.
* ``state_delta`` is ``JSONB`` adapted through ``psycopg2.extras.Json``, and the
  exposure declares it in ``json_columns``. The delta must carry it as a JSON
  OBJECT, not as a re-encoded JSON string the reader would have to parse twice.
* ``UNIQUE (session_id, sequence)`` is a real constraint that the in-memory
  double does not enforce. A redelivery must reproduce the SAME key and the SAME
  row rather than issuing a new ordinal, and that is only actually proven where
  the constraint exists.

The publish transport is injected as a recording double on purpose: this file is
a proof about the ROW the encoder sees and the KEY it derives, not about
aiokafka. The broker leg is exercised live on the dev lane in the ticket's
rendered-page evidence.

## Skip contract

SKIPS (never ERRORs) without a reachable database or an applied relation,
mirroring ``tests/test_omn17183_real_postgres_session_replay_write_path.py``.
Every row it writes is scoped to a synthetic ``omn17774-`` session id and
deleted on the way out.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import quote_plus

import pytest

from omnimarket.nodes.node_projection_session_replay.handlers.handler_projection_session_replay import (
    TABLE,
    TOPIC_TOOL_EXECUTED,
    HandlerProjectionSessionReplay,
)
from omnimarket.projection.postgres_sync_database import PostgresSyncProjectionAdapter
from omnimarket.projection.snapshot_publisher import ModelSnapshotDeltaMessage

_QUALIFIED = f"public.{TABLE}"
_SESSION_PREFIX = "omn17774-real-pg"
_TOPIC = "onex.snapshot.projection.session.replay.v1"


class _RecordingPublisher:
    """Captures encoded deltas instead of reaching a broker."""

    def __init__(self) -> None:
        self.messages: list[ModelSnapshotDeltaMessage] = []

    def publish(self, message: ModelSnapshotDeltaMessage) -> bool:
        self.messages.append(message)
        return True


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
            "POSTGRES_PASSWORD not set -- skipping OMN-17774 real-Postgres "
            "session_replay snapshot-publish proof"
        )
    psycopg2 = pytest.importorskip("psycopg2")
    try:
        conn = psycopg2.connect(_dsn())  # no-contract-check: test harness probe
    except psycopg2.Error as exc:  # pragma: no cover - infrastructure dependent
        pytest.skip(f"no reachable Postgres for OMN-17774 snapshot publish: {exc}")
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
        cur.execute(f"DELETE FROM {_QUALIFIED} WHERE session_id = %s", (session_id,))


def _stored(conn: Any, session_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT snapshot_id, sequence, event_type, node_name, state_delta, "
            f"cumulative_tokens, is_checkpoint FROM {_QUALIFIED} "
            "WHERE session_id = %s ORDER BY sequence ASC",
            (session_id,),
        )
        return [
            {
                "snapshot_id": str(row[0]),
                "sequence": row[1],
                "event_type": row[2],
                "node_name": row[3],
                "state_delta": row[4],
                "cumulative_tokens": row[5],
                "is_checkpoint": row[6],
            }
            for row in cur.fetchall()
        ]


def _dispatch(
    handler: HandlerProjectionSessionReplay,
    adapter: PostgresSyncProjectionAdapter,
    session_id: str,
    **payload: Any,
) -> dict[str, object]:
    """Drive handle() through the PRODUCTION sync adapter, the runtime's shape."""
    return handler.handle(
        {
            "session_id": session_id,
            "_db": adapter,
            "_topic": TOPIC_TOOL_EXECUTED,
            **payload,
        }
    )


@pytest.mark.integration
def test_the_delta_key_is_the_stored_snapshot_id_and_the_row_matches() -> None:
    """The bus carries exactly what Postgres stored, keyed by the stored identity."""
    conn = _connect_or_skip()
    session_id = f"{_SESSION_PREFIX}-key"
    try:
        _ensure_table_or_skip(conn)
        try:
            _purge(conn, session_id)
            publisher = _RecordingPublisher()
            handler = HandlerProjectionSessionReplay(publisher=publisher)
            adapter = PostgresSyncProjectionAdapter(_dsn())

            result = _dispatch(
                handler,
                adapter,
                session_id,
                timestamp="2026-09-03T09:00:02+00:00",
                tool_name="Read",
                tool_input={"path": "README.md"},
                tokens_used=8,
            )
            assert result["rows_upserted"] == 1
            assert result["snapshot_published"] is True

            rows = _stored(conn, session_id)
            assert len(rows) == 1, "the real write path produced no row"
            stored = rows[0]

            assert len(publisher.messages) == 1
            message = publisher.messages[0]
            assert message.topic == _TOPIC
            # The declared key_column is snapshot_id, and the key on the wire must
            # be the identity Postgres actually holds -- not a value derived a
            # second time from the event, which could diverge from the stored row.
            assert message.key == stored["snapshot_id"].encode("utf-8")

            assert message.value is not None
            delta = json.loads(message.value)
            assert delta["op"] == "upsert"
            assert delta["key"] == [stored["snapshot_id"]]
            assert delta["row"]["sequence"] == stored["sequence"]
            assert delta["row"]["event_type"] == stored["event_type"]
            assert delta["row"]["node_name"] == stored["node_name"]
            assert delta["row"]["cumulative_tokens"] == stored["cumulative_tokens"]
            assert delta["row"]["is_checkpoint"] == stored["is_checkpoint"]
            # JSONB round-trips as a dict on the psycopg2 side; the exposure
            # declares state_delta in json_columns, so the delta must carry an
            # OBJECT and not a re-encoded string.
            assert isinstance(delta["row"]["state_delta"], dict)
            assert delta["row"]["state_delta"] == stored["state_delta"]
            # TIMESTAMPTZ column, ISO-8601 string in: the value on the bus is the
            # one the handler wrote, and it must still be a string the
            # SnapshotCache and the page can render without a driver.
            assert isinstance(delta["row"]["timestamp"], str)
        finally:
            _purge(conn, session_id)
    finally:
        conn.close()


@pytest.mark.integration
def test_a_redelivery_republishes_one_key_under_the_real_unique_constraint() -> None:
    """Redelivery is an idempotent republish, not a second ordinal.

    ``UNIQUE (session_id, sequence)`` only exists in real Postgres. A rehydration
    that issued a fresh ordinal for an already-materialized event would raise
    ``UniqueViolation`` here -- and would also put a SECOND key on the compacted
    topic for one source event, which is the property the exposure's fixed
    ordering token depends on.
    """
    conn = _connect_or_skip()
    session_id = f"{_SESSION_PREFIX}-redelivery"
    envelope_id = "9f1c1f6e-0000-4000-8000-00000000abcd"
    try:
        _ensure_table_or_skip(conn)
        try:
            _purge(conn, session_id)
            publisher = _RecordingPublisher()
            handler = HandlerProjectionSessionReplay(publisher=publisher)
            adapter = PostgresSyncProjectionAdapter(_dsn())

            for _ in range(2):
                result = _dispatch(
                    handler,
                    adapter,
                    session_id,
                    timestamp="2026-09-03T09:00:03+00:00",
                    tool_name="Bash",
                    tool_input={"command": "ls"},
                    tokens_used=5,
                    _envelope_id=envelope_id,
                )
                assert result["rows_upserted"] == 1

            rows = _stored(conn, session_id)
            assert len(rows) == 1, "a redelivery materialized a second row"
            assert rows[0]["sequence"] == 0

            assert len(publisher.messages) == 2
            first, second = publisher.messages
            assert first.key == second.key == rows[0]["snapshot_id"].encode("utf-8")
            assert first.value is not None
            assert second.value is not None
            assert json.loads(first.value)["row"] == json.loads(second.value)["row"]
        finally:
            _purge(conn, session_id)
    finally:
        conn.close()
