# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16180: real-Postgres write-path proof for ``omninode_internal.work_events``.

## Why a mock-backed test is not enough here

``InmemoryDatabaseAdapter`` accepts any Python object for any column. The
handler hands ``emitted_at`` to the adapter as an **ISO-8601 string** while the
column is ``TIMESTAMPTZ``, and ``payload`` as a **dict** while the column is
``JSONB``. Both of those are exactly the str-vs-column-type class OMN-15905
cost a deploy cycle to find, and neither is observable without a real
connection -- which is why ``projection-write-path-db-gate`` requires this file
to exist beside any change to a projection write path.

The suite SKIPS (never ERRORs) without a reachable database, mirroring
``tests/test_omn15909_real_postgres_projection_write_path_gate.py``.

## What it proves

1. The row the handler builds is **accepted by the real column types** --
   TIMESTAMPTZ and JSONB both parse, no implicit-cast failure.
2. ``emitted_at`` survives the round trip **as the same instant**, not shifted
   by the server's timezone.
3. Replaying the same event is **idempotent on the real ON CONFLICT path**, not
   merely in the in-memory double's merge semantics.
4. Two distinct events do **not** collide -- the defect the sibling
   ``session_replay_snapshots`` ships today, where a key derived from an
   unthreaded ``sequence`` collapses a whole session onto one row.
5. ``handle()`` -- the method the runtime actually calls, not the ``accumulate``
   helper the first four tests drive -- writes a real row through a real
   ``ProtocolProjectionDatabaseSync`` implementation. This closes a coverage
   gap the first version of this file left open: every real-DB assertion ran
   against ``accumulate()`` + hand-written SQL, so the runtime entrypoint
   (``handler_wiring._invoke_projection`` calls ``handle(input_data)``, never
   ``accumulate``/``project``) had no real-Postgres proof at all. Its parameter
   name is load-bearing for the OMN-14355 canon-shape ratchet, and its
   ``_db``/``_topic`` unpacking is the seam ``split_projection_input`` exists to
   keep from drifting -- both are now exercised against real column types.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from urllib.parse import quote_plus

import asyncpg
import pytest

from omnimarket.nodes.node_projection_work_events.handlers.handler_projection_work_events import (
    SCHEMA,
    TABLE,
    TOPIC_TOOL_EXECUTED,
    HandlerProjectionWorkEvents,
)
from omnimarket.nodes.node_projection_work_events.models.model_work_event import (
    ModelWorkEventInbound,
)
from omnimarket.projection.snapshot_publisher import ModelSnapshotDeltaMessage

_QUALIFIED = f"{SCHEMA}.{TABLE}"
_SESSION = "omn16180-real-pg-write-path"


def _base_dsn() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnidash_analytics")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


def _dsn_or_skip() -> str:
    """The same skip contract as :func:`_connect_or_skip`, for sync callers.

    Returns the DSN rather than an open connection because the synchronous
    ``ProtocolProjectionDatabaseSync`` implementation below opens and closes its
    own connection per ``upsert``, mirroring the production ownership rule.
    """
    creds = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not creds:
        pytest.skip(
            "POSTGRES_PASSWORD not set -- skipping OMN-16180 real-Postgres "
            "work_events write-path proof"
        )
    dsn = _base_dsn()

    async def _probe() -> None:
        conn = await asyncpg.connect(dsn)
        await conn.close()

    try:
        asyncio.run(_probe())
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for OMN-16180 write path: {exc}")
    return dsn


async def _connect_or_skip() -> asyncpg.Connection:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip(
            "POSTGRES_PASSWORD not set -- skipping OMN-16180 real-Postgres "
            "work_events write-path proof"
        )
    try:
        return await asyncpg.connect(_base_dsn())
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for OMN-16180 write path: {exc}")


async def _ensure_table_or_skip(conn: asyncpg.Connection) -> None:
    """Skip -- never fail -- when the relation has not been applied here.

    MUST be awaited OUTSIDE any ``try`` whose ``finally`` touches the table.
    ``pytest.skip`` raises, so a cleanup ``DELETE FROM omninode_internal.
    work_events`` in such a ``finally`` runs anyway, raises
    ``UndefinedTableError``, and REPLACES the skip with a failure -- which is
    exactly what happened on the ephemeral CI Postgres, where the relation is
    absent because 0001 is vendored into omnibase_infra and never applied by
    omnimarket's own fixture. The nesting below keeps the cleanup inside the
    branch where the table is already proven to exist.
    """
    exists = await conn.fetchval(
        "SELECT to_regclass($1) IS NOT NULL", f"{SCHEMA}.{TABLE}"
    )
    if not exists:
        pytest.skip(
            f"{_QUALIFIED} not present -- apply "
            "node_projection_work_events/0001_create_work_events.sql first"
        )


def _event(emitted_at: str, tool: str) -> ModelWorkEventInbound:
    return ModelWorkEventInbound(
        session_id=_SESSION,
        emitted_at=emitted_at,
        working_directory="omni_home",
        tool_name=tool,
        duration_ms=184,
        interrupted=False,
        hook_source="post_tool_use",
    )


async def _upsert(conn: asyncpg.Connection, event: ModelWorkEventInbound) -> str:
    """Write through the handler's own row shape, via the real column types."""
    row = HandlerProjectionWorkEvents().accumulate(event, TOPIC_TOOL_EXECUTED)
    await conn.execute(
        f"""
        INSERT INTO {_QUALIFIED}
          (event_id, emitted_at, event_kind, actor_kind, actor_id, ticket_id,
           summary, source_topic, payload)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
        ON CONFLICT (event_id) DO UPDATE SET
          emitted_at = EXCLUDED.emitted_at,
          summary = EXCLUDED.summary,
          payload = EXCLUDED.payload
        """,
        row.event_id,
        # The handler's OWN value, unmodified. It must be a real datetime:
        # asyncpg refuses a str for a TIMESTAMPTZ parameter, which is precisely
        # the defect this file caught when project() serialized with
        # .isoformat(). Passing row.emitted_at through untouched keeps that
        # regression detectable here.
        row.emitted_at,
        row.event_kind,
        row.actor_kind.value,
        row.actor_id,
        row.ticket_id,
        row.summary,
        row.source_topic,
        json.dumps(row.payload, sort_keys=True),
    )
    return row.event_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_column_types_accept_the_handler_row_and_round_trip() -> None:
    conn = await _connect_or_skip()
    try:
        # OUTSIDE the cleanup try below, deliberately -- see
        # _ensure_table_or_skip's docstring. A skip raised inside it would be
        # overwritten by the cleanup DELETE's UndefinedTableError.
        await _ensure_table_or_skip(conn)
        await conn.execute(f"DELETE FROM {_QUALIFIED} WHERE actor_id = $1", _SESSION)

        emitted = "2026-08-30T01:59:07.891697+00:00"
        event_id = await _upsert(conn, _event(emitted, "Bash"))

        stored = await conn.fetchrow(
            f"SELECT emitted_at, payload, summary, event_kind FROM {_QUALIFIED} "
            "WHERE event_id = $1",
            event_id,
        )
        assert stored is not None, "the real write path produced no row"

        # TIMESTAMPTZ parsed the handler's ISO string, and the instant is intact.
        assert stored["emitted_at"] == datetime.fromisoformat(emitted).astimezone(UTC)
        # JSONB parsed the handler's payload and preserved its fields.
        assert json.loads(stored["payload"])["tool_name"] == "Bash"
        assert stored["summary"] == "tool Bash (184 ms)"
        assert stored["event_kind"] == "session.tool"
    finally:
        if await conn.fetchval(
            "SELECT to_regclass($1) IS NOT NULL", f"{SCHEMA}.{TABLE}"
        ):
            await conn.execute(
                f"DELETE FROM {_QUALIFIED} WHERE actor_id = $1", _SESSION
            )
        await conn.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_is_idempotent_and_distinct_events_do_not_collide() -> None:
    conn = await _connect_or_skip()
    try:
        # Same ordering constraint as the test above.
        await _ensure_table_or_skip(conn)
        await conn.execute(f"DELETE FROM {_QUALIFIED} WHERE actor_id = $1", _SESSION)

        first = _event("2026-08-30T01:59:07.891697+00:00", "Bash")
        await _upsert(conn, first)
        await _upsert(conn, first)  # real ON CONFLICT path, not a double's merge
        after_replay = await conn.fetchval(
            f"SELECT count(*) FROM {_QUALIFIED} WHERE actor_id = $1", _SESSION
        )
        assert after_replay == 1, "replaying one event must not add a row"

        # A second, genuinely different event must NOT be absorbed -- this is the
        # collapse defect session_replay_snapshots exhibits live.
        await _upsert(conn, _event("2026-08-30T02:00:00+00:00", "Read"))
        after_second = await conn.fetchval(
            f"SELECT count(*) FROM {_QUALIFIED} WHERE actor_id = $1", _SESSION
        )
        assert after_second == 2, "two distinct events collapsed onto one row"
    finally:
        if await conn.fetchval(
            "SELECT to_regclass($1) IS NOT NULL", f"{SCHEMA}.{TABLE}"
        ):
            await conn.execute(
                f"DELETE FROM {_QUALIFIED} WHERE actor_id = $1", _SESSION
            )
        await conn.close()


class _RealPostgresUpsertAdapter:
    """Minimal real-Postgres ``ProtocolProjectionDatabaseSync`` for one test.

    Deliberately NOT ``InmemoryDatabaseAdapter``: the point is that the values
    ``handle()`` -> ``project()`` hand to ``upsert`` reach real TIMESTAMPTZ and
    JSONB columns. It is structurally typed against the protocol, so
    ``handle()``'s ``isinstance(db_raw, DatabaseAdapter)`` guard accepts it
    exactly as it accepts the production adapter.

    ``upsert`` is synchronous -- the protocol's shape -- and opens/closes its own
    asyncpg connection inside ``asyncio.run``, which is the same ownership rule
    OMN-16874 established for the production path ("a pool's lifetime belongs to
    the loop that uses it, and that loop is opened inside the handler"). Every
    value except ``payload`` is bound through UNTOUCHED, so a str-vs-datetime
    regression on ``emitted_at`` still fails loudly here.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def upsert(
        self,
        table: str,
        conflict_key: str,
        row: dict[str, object],
    ) -> bool:
        self.calls.append((table, conflict_key, dict(row)))

        async def _write() -> None:
            conn = await asyncpg.connect(self._dsn)
            try:
                await conn.execute(
                    f"""
                    INSERT INTO {_QUALIFIED}
                      (event_id, emitted_at, event_kind, actor_kind, actor_id,
                       ticket_id, summary, source_topic, payload)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
                    ON CONFLICT ({conflict_key}) DO UPDATE SET
                      emitted_at = EXCLUDED.emitted_at,
                      summary = EXCLUDED.summary,
                      payload = EXCLUDED.payload
                    """,
                    row["event_id"],
                    row["emitted_at"],
                    row["event_kind"],
                    row["actor_kind"],
                    row["actor_id"],
                    row["ticket_id"],
                    row["summary"],
                    row["source_topic"],
                    # json.dumps here, not in the handler: asyncpg binds a dict
                    # to JSONB only through a codec, and the production adapter
                    # performs this same serialization.
                    json.dumps(row["payload"], sort_keys=True),
                )
            finally:
                await conn.close()

        asyncio.run(_write())
        return True

    def query(
        self,
        table: str,
        filters: dict[str, object] | None = None,
    ) -> list[dict[str, object]]:
        raise NotImplementedError("node_projection_work_events never reads")


@pytest.mark.integration
def test_handle_the_runtime_entrypoint_writes_through_real_column_types() -> None:
    """``handle()`` -- not ``accumulate()`` -- against a real database.

    ``handler_wiring._invoke_projection`` calls ``handle(input_data)`` with
    ``_db``/``_topic``/``_event_type`` injected into the event payload dict. The
    other tests in this file drive ``accumulate()`` plus hand-written SQL, so
    that entrypoint had no real-DB proof: a regression in its ``_db``/``_topic``
    unpacking, in its parameter name (load-bearing for the OMN-14355 canon-shape
    ratchet), or in ``project()``'s row construction would have been invisible
    here. This closes that gap.
    """
    dsn = _dsn_or_skip()

    async def _prepare() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await _ensure_table_or_skip(conn)
            await conn.execute(
                f"DELETE FROM {_QUALIFIED} WHERE actor_id = $1", _SESSION
            )
        finally:
            await conn.close()

    asyncio.run(_prepare())

    emitted = "2026-08-30T03:14:09.123456+00:00"
    event = _event(emitted, "Bash")
    adapter = _RealPostgresUpsertAdapter(dsn)

    # Exactly the shape the runtime injects: the event payload plus the
    # bookkeeping keys, splatted positionally into handle().
    payload: dict[str, object] = event.model_dump(mode="json")
    payload["_db"] = adapter
    payload["_topic"] = TOPIC_TOOL_EXECUTED
    payload["_event_type"] = "tool-executed"

    result = HandlerProjectionWorkEvents().handle(payload)
    assert result["rows_upserted"] == 1, result

    # The handler must have handed the adapter a real datetime, never a str --
    # the OMN-15905 class this file exists for.
    assert len(adapter.calls) == 1
    _, _, upserted = adapter.calls[0]
    assert isinstance(upserted["emitted_at"], datetime), (
        "project() passed a non-datetime to upsert(); asyncpg refuses a str for "
        "a TIMESTAMPTZ parameter"
    )

    async def _readback() -> asyncpg.Record | None:
        conn = await asyncpg.connect(dsn)
        try:
            return await conn.fetchrow(
                f"SELECT emitted_at, event_kind, summary, payload FROM {_QUALIFIED} "
                "WHERE actor_id = $1",
                _SESSION,
            )
        finally:
            await conn.close()

    try:
        stored = asyncio.run(_readback())
        assert stored is not None, "handle() reported success but wrote no row"
        assert stored["emitted_at"] == datetime.fromisoformat(emitted).astimezone(UTC)
        assert stored["event_kind"] == "session.tool"
        assert stored["summary"] == "tool Bash (184 ms)"
        assert json.loads(stored["payload"])["tool_name"] == "Bash"
    finally:

        async def _cleanup() -> None:
            conn = await asyncpg.connect(dsn)
            try:
                await conn.execute(
                    f"DELETE FROM {_QUALIFIED} WHERE actor_id = $1", _SESSION
                )
            finally:
                await conn.close()

        asyncio.run(_cleanup())


@pytest.mark.integration
def test_handle_publishes_the_row_the_real_database_actually_stored() -> None:
    """OMN-17772: the snapshot delta must carry the row Postgres accepted.

    The whole point of the bus-backed exposure is that the read model shows
    what the ledger holds. Two type conversions sit between those, and neither
    is observable against ``InmemoryDatabaseAdapter``:

    * ``emitted_at`` is handed to the adapter as a real ``datetime`` (asyncpg
      refuses a str for TIMESTAMPTZ -- the OMN-15905 class this file exists
      for) and must be JSON-serialized for the delta;
    * ``payload`` is a dict against a ``JSONB`` column and must round-trip
      through ``snapshot_json_value`` without being double-encoded.

    So this asserts field-by-field that what was PUBLISHED equals what was
    READ BACK OUT OF POSTGRES -- not that a publish merely happened. A delta
    that disagrees with the stored row is the split-brain the exposure exists
    to close, and it can only be caught with real column types on one side.
    """
    dsn = _dsn_or_skip()

    async def _prepare() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await _ensure_table_or_skip(conn)
            await conn.execute(
                f"DELETE FROM {_QUALIFIED} WHERE actor_id = $1", _SESSION
            )
        finally:
            await conn.close()

    asyncio.run(_prepare())

    published: list[tuple[str, bytes | None, bytes]] = []

    class _CapturingPublisher:
        """Records the encoded delta; ``encode_snapshot_delta`` builds it.

        Injected through the handler's own constructor seam
        (``ProtocolSnapshotDeltaPublisher``, OMN-17774) rather than patched
        onto a module attribute, so this test exercises the same wiring the
        runtime uses.
        """

        def publish(self, message: ModelSnapshotDeltaMessage) -> bool:
            published.append((message.topic, message.value, message.key))
            return True

    emitted = "2026-08-30T03:14:09.123456+00:00"
    handler = HandlerProjectionWorkEvents(publisher=_CapturingPublisher())
    adapter = _RealPostgresUpsertAdapter(dsn)
    payload: dict[str, object] = _event(emitted, "Bash").model_dump(mode="json")
    payload["_db"] = adapter
    payload["_topic"] = TOPIC_TOOL_EXECUTED
    payload["_event_type"] = "tool-executed"

    result = handler.handle(payload)

    assert result["rows_upserted"] == 1, result
    assert len(published) == 1, "one delta per accepted row"
    topic, value, key = published[0]
    assert topic == "onex.snapshot.projection.work.events.v1"
    assert value is not None

    async def _readback() -> asyncpg.Record | None:
        conn = await asyncpg.connect(dsn)
        try:
            return await conn.fetchrow(
                f"SELECT event_id, emitted_at, event_kind, actor_kind, actor_id, "
                f"summary, source_topic, payload FROM {_QUALIFIED} "
                "WHERE actor_id = $1",
                _SESSION,
            )
        finally:
            await conn.close()

    try:
        stored = asyncio.run(_readback())
        assert stored is not None, "handle() reported success but wrote no row"
        delta = json.loads(value)
        row = delta["row"]

        assert key.decode("utf-8") == stored["event_id"], (
            "the message key must be the stored row's own content address"
        )
        assert delta["key"] == [stored["event_id"]]
        assert row["event_id"] == stored["event_id"]
        assert row["event_kind"] == stored["event_kind"]
        assert row["actor_kind"] == stored["actor_kind"]
        assert row["actor_id"] == stored["actor_id"]
        assert row["summary"] == stored["summary"]
        assert row["source_topic"] == stored["source_topic"]
        # TIMESTAMPTZ out of Postgres vs the published ISO string: the same
        # instant, compared as instants so a server timezone cannot hide a
        # shift behind a string mismatch.
        assert datetime.fromisoformat(str(row["emitted_at"])).astimezone(UTC) == stored[
            "emitted_at"
        ].astimezone(UTC)
        # JSONB out vs the published payload: a real mapping on both sides,
        # never a JSON string that was encoded twice.
        assert isinstance(row["payload"], dict)
        assert row["payload"] == json.loads(stored["payload"])
        assert row["payload"]["tool_name"] == "Bash"
    finally:

        async def _cleanup() -> None:
            conn = await asyncpg.connect(dsn)
            try:
                await conn.execute(
                    f"DELETE FROM {_QUALIFIED} WHERE actor_id = $1", _SESSION
                )
            finally:
                await conn.close()

        asyncio.run(_cleanup())
