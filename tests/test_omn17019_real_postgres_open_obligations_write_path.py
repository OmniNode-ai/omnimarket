# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17019: real-Postgres write-path proof for ``omninode_internal.open_obligations``.

## Why a mock-backed test cannot cover this node at all

``InmemoryDatabaseAdapter`` accepts any Python object for any column and has no
notion of a GENERATED column. This node's two most important columns --
``state`` and ``owed_by`` -- exist ONLY as Postgres expressions. Every claim
this ticket makes about "what is currently owed" is a claim about values the
in-memory double cannot compute, so without this file the projection's central
behaviour would be entirely unproven. That is on top of the str-vs-column-type
class (OMN-15905) ``projection-write-path-db-gate`` exists to catch: the handler
hands ``last_event_at`` to the adapter as a real ``datetime`` for a TIMESTAMPTZ
column and ``payload`` as a dict for JSONB.

The suite SKIPS (never ERRORs) without a reachable database, mirroring
``tests/test_omn16180_real_postgres_work_events_write_path.py``.

## What it proves

1. The row the handler builds is accepted by the real column types.
2. ``state`` really is derived: after ``created`` the row reads ``open``, after
   ``satisfied`` it reads ``satisfied``, and nothing ever wrote the column.
3. ``owed_by`` really is derived: a transfer moves it while
   ``original_owed_by`` stays on the record.
4. **The replay case.** Re-applying ``created`` after ``satisfied`` -- exactly
   what a consumer restarting from an earlier partition offset does -- leaves
   the obligation closed, on the real ``ON CONFLICT`` path rather than in the
   double's merge semantics.
5. The generated columns are not writable: an UPDATE naming ``state`` is
   rejected by Postgres, so no replay, no migration and no hand-run statement
   can make the projection disagree with the events it folded.
6. ``handle()`` -- the method the runtime actually calls, not the ``accumulate``
   helper -- writes a real row through a real ``ProtocolProjectionDatabaseSync``
   implementation.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from urllib.parse import quote_plus

import asyncpg
import pytest

from omnimarket.nodes.node_projection_open_obligations.handlers.handler_projection_open_obligations import (
    _TOPIC_KIND,
    SCHEMA,
    TABLE,
    HandlerProjectionOpenObligations,
)
from omnimarket.nodes.node_projection_open_obligations.models.model_obligation_event import (
    EnumObligationEventKind,
    EnumObligationState,
    ModelObligationEventInbound,
)

_QUALIFIED = f"{SCHEMA}.{TABLE}"
_OBLIGATION = "omn17019-real-pg-write-path"
_TOPIC_BY_KIND: dict[EnumObligationEventKind, str] = {
    kind: topic for topic, kind in _TOPIC_KIND.items()
}


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
    """Return a reachable DSN, or skip.

    Returns the DSN rather than an open connection because the synchronous
    ``ProtocolProjectionDatabaseSync`` implementation below opens and closes its
    own connection per ``upsert``, mirroring the production ownership rule.
    """
    if not os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    ):
        pytest.skip(
            "POSTGRES_PASSWORD not set -- skipping OMN-17019 real-Postgres "
            "open_obligations write-path proof"
        )
    dsn = _base_dsn()

    async def _probe() -> None:
        conn = await asyncpg.connect(dsn)
        await conn.close()

    try:
        asyncio.run(_probe())
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for OMN-17019 write path: {exc}")
    return dsn


async def _ensure_table_or_skip(conn: asyncpg.Connection) -> None:
    """Skip -- never fail -- when the relation has not been applied here.

    MUST be awaited OUTSIDE any ``try`` whose ``finally`` touches the table:
    ``pytest.skip`` raises, so a cleanup ``DELETE`` in such a ``finally`` runs
    anyway, raises ``UndefinedTableError``, and REPLACES the skip with a
    failure. The sibling OMN-16180 file records that exact bite.
    """
    exists = await conn.fetchval(
        "SELECT to_regclass($1) IS NOT NULL", f"{SCHEMA}.{TABLE}"
    )
    if not exists:
        pytest.skip(
            f"{_QUALIFIED} not present -- apply "
            "node_projection_open_obligations/0001_create_open_obligations.sql first"
        )


class _RealPostgresUpsertAdapter:
    """Minimal real-Postgres ``ProtocolProjectionDatabaseSync`` for these tests.

    Structurally typed against the protocol, so ``handle()``'s
    ``isinstance(db_raw, DatabaseAdapter)`` guard accepts it exactly as it
    accepts the production adapter.

    The INSERT column list is built FROM THE INCOMING ROW rather than hardcoded,
    which is the point: this node's whole correctness argument is that an event
    writes only the columns its kind owns and the ``ON CONFLICT DO UPDATE`` sets
    only those. A hardcoded column list here would silently write NULLs over
    columns the handler deliberately omitted, and the replay test below would
    pass against a projection that is actually broken.
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
        columns = list(row)
        placeholders = ", ".join(
            # json.dumps + ::jsonb here, not in the handler: asyncpg binds a
            # dict to JSONB only through a codec, and the production adapter
            # performs the same serialization.
            f"${index}::jsonb" if column == "payload" else f"${index}"
            for index, column in enumerate(columns, start=1)
        )
        updates = ", ".join(
            f"{column} = EXCLUDED.{column}"
            for column in columns
            if column != conflict_key
        )
        values = [
            json.dumps(row[column], sort_keys=True)
            if column == "payload"
            else row[column]
            for column in columns
        ]

        async def _write() -> None:
            conn = await asyncpg.connect(self._dsn)
            try:
                await conn.execute(
                    f"INSERT INTO {_QUALIFIED} ({', '.join(columns)}) "
                    f"VALUES ({placeholders}) "
                    f"ON CONFLICT ({conflict_key}) DO UPDATE SET {updates}",
                    *values,
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
        raise NotImplementedError("node_projection_open_obligations never reads")


def _event(
    kind: EnumObligationEventKind, emitted_at: str
) -> ModelObligationEventInbound:
    payload: dict[str, object] = {
        "obligation_id": _OBLIGATION,
        "emitted_at": emitted_at,
        "actor_id": "omn17019-real-pg",
        "summary": f"{kind.value} for the real-Postgres write-path proof",
    }
    payload.update(
        {
            EnumObligationEventKind.CREATED: {
                "asked_by": "operator",
                "owed_by": "session-morning",
                "acceptance_condition": "brief delivered and acknowledged",
            },
            EnumObligationEventKind.TRANSFERRED: {"owed_by": "session-afternoon"},
            EnumObligationEventKind.SATISFIED: {
                "evidence_uri": "https://example.invalid/brief.md",
                "delivery_state": "sent",
            },
            EnumObligationEventKind.SUPERSEDED: {
                "superseded_by_obligation_id": "omn17019-successor"
            },
            EnumObligationEventKind.ABANDONED: {"abandon_reason": "withdrawn"},
        }[kind]
    )
    return ModelObligationEventInbound(**payload)  # type: ignore[arg-type]


def _apply(
    adapter: _RealPostgresUpsertAdapter, kind: EnumObligationEventKind, when: str
) -> None:
    HandlerProjectionOpenObligations().project(
        _event(kind, when), adapter, _TOPIC_BY_KIND[kind]
    )


async def _fetch(conn: asyncpg.Connection) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"SELECT * FROM {_QUALIFIED} WHERE obligation_id = $1", _OBLIGATION
    )


async def _cleanup(conn: asyncpg.Connection) -> None:
    """Remove only this test's own row.

    The migration grants the RUNTIME no DELETE by design; this cleanup runs as
    the test's own connecting role, not as omninode_runtime, and touches exactly
    one synthetic obligation id.
    """
    await conn.execute(
        f"DELETE FROM {_QUALIFIED} WHERE obligation_id = $1", _OBLIGATION
    )


@pytest.mark.integration
def test_state_and_owed_by_are_derived_across_a_real_lifecycle() -> None:
    """The central claim: the projection computes what is owed, nobody sets it."""
    dsn = _dsn_or_skip()
    adapter = _RealPostgresUpsertAdapter(dsn)

    async def _run() -> None:
        conn = await asyncpg.connect(dsn)
        await _ensure_table_or_skip(conn)
        try:
            await _cleanup(conn)

            _apply(
                adapter, EnumObligationEventKind.CREATED, "2026-08-30T09:00:00+00:00"
            )
            row = await _fetch(conn)
            assert row is not None
            assert row["state"] == EnumObligationState.OPEN.value
            assert row["owed_by"] == "session-morning"
            assert row["closed_state"] is None

            _apply(
                adapter,
                EnumObligationEventKind.TRANSFERRED,
                "2026-08-30T11:30:00+00:00",
            )
            row = await _fetch(conn)
            assert row is not None
            assert row["state"] == EnumObligationState.OPEN.value
            assert row["owed_by"] == "session-afternoon"
            assert row["original_owed_by"] == "session-morning"

            _apply(
                adapter, EnumObligationEventKind.SATISFIED, "2026-08-30T16:45:00+00:00"
            )
            row = await _fetch(conn)
            assert row is not None
            assert row["state"] == EnumObligationState.SATISFIED.value
            assert row["evidence_uri"] == "https://example.invalid/brief.md"
            assert row["delivery_state"] == "sent"
            # The opening facts survived every later write.
            assert row["asked_by"] == "operator"
            assert row["acceptance_condition"] == "brief delivered and acknowledged"

            # No adapter call ever named a derived column.
            for _table, _key, written in adapter.calls:
                assert "state" not in written
                assert "owed_by" not in written
        finally:
            await _cleanup(conn)
            await conn.close()

    asyncio.run(_run())


@pytest.mark.integration
def test_replayed_created_after_satisfied_does_not_reopen_on_real_on_conflict() -> None:
    """A rewound consumer must not resurrect a delivered obligation."""
    dsn = _dsn_or_skip()
    adapter = _RealPostgresUpsertAdapter(dsn)

    async def _run() -> None:
        conn = await asyncpg.connect(dsn)
        await _ensure_table_or_skip(conn)
        try:
            await _cleanup(conn)
            _apply(
                adapter, EnumObligationEventKind.CREATED, "2026-08-30T09:00:00+00:00"
            )
            _apply(
                adapter, EnumObligationEventKind.SATISFIED, "2026-08-30T16:45:00+00:00"
            )
            _apply(
                adapter, EnumObligationEventKind.CREATED, "2026-08-30T09:00:00+00:00"
            )

            row = await _fetch(conn)
            assert row is not None
            assert row["state"] == EnumObligationState.SATISFIED.value
            assert row["evidence_uri"] == "https://example.invalid/brief.md"
        finally:
            await _cleanup(conn)
            await conn.close()

    asyncio.run(_run())


@pytest.mark.integration
def test_the_derived_columns_cannot_be_written_by_anyone() -> None:
    """Postgres itself refuses an UPDATE that sets a generated column.

    This is the assertion that makes "the projection cannot disagree with its
    events" a database-enforced property rather than a handler convention.
    """
    dsn = _dsn_or_skip()
    adapter = _RealPostgresUpsertAdapter(dsn)

    async def _run() -> None:
        conn = await asyncpg.connect(dsn)
        await _ensure_table_or_skip(conn)
        try:
            await _cleanup(conn)
            _apply(
                adapter, EnumObligationEventKind.CREATED, "2026-08-30T09:00:00+00:00"
            )
            with pytest.raises(asyncpg.PostgresError):
                await conn.execute(
                    f"UPDATE {_QUALIFIED} SET state = 'satisfied' "
                    "WHERE obligation_id = $1",
                    _OBLIGATION,
                )
        finally:
            await _cleanup(conn)
            await conn.close()

    asyncio.run(_run())


@pytest.mark.integration
def test_handle_writes_real_column_types_through_the_runtime_entrypoint() -> None:
    """``handle()`` is what the runtime calls; prove IT against real columns."""
    dsn = _dsn_or_skip()
    adapter = _RealPostgresUpsertAdapter(dsn)

    async def _run() -> None:
        conn = await asyncpg.connect(dsn)
        await _ensure_table_or_skip(conn)
        try:
            await _cleanup(conn)
            request = _event(
                EnumObligationEventKind.CREATED, "2026-08-30T09:00:00+00:00"
            ).model_dump(mode="python")
            request["_db"] = adapter
            request["_topic"] = _TOPIC_BY_KIND[EnumObligationEventKind.CREATED]
            result = HandlerProjectionOpenObligations().handle(request)
            assert result["rows_upserted"] == 1

            row = await _fetch(conn)
            assert row is not None
            # A real TIMESTAMPTZ round trip -- the same instant, not shifted by
            # the server's timezone, and not a string that happened to cast.
            assert row["last_event_at"] == datetime(2026, 8, 30, 9, 0, tzinfo=UTC)
            assert row["created_at"] == datetime(2026, 8, 30, 9, 0, tzinfo=UTC)
            assert isinstance(row["payload"], str | dict)
        finally:
            await _cleanup(conn)
            await conn.close()

    asyncio.run(_run())
