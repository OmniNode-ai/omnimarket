# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16777: real-Postgres write-path gate for the consumer-flow projection.

Why this is not redundant with the in-memory suites: those drive the same
derivation against ``InmemoryDatabaseAdapter``, which accepts a bound parameter
of ANY Python type — a ``.isoformat()`` string binds as "successfully" as a real
``datetime``. Only a real Postgres connection enforces column types through
asyncpg's extended query protocol. That gap is exactly how the OMN-15905
str-where-``TIMESTAMPTZ`` defect reached a merged, deployed, CrashLoopBackOff-ing
runtime with every layer of mock-DB coverage green.

Two things here need real Postgres specifically and cannot be faked:

1. **NULL is not 0.** The whole AC5 claim is that a dropped window materializes
   as ``UNKNOWN`` with NULL counters. That is only meaningful if the columns are
   genuinely nullable in the real schema — an in-memory dict stores ``None`` in a
   column that a ``NOT NULL DEFAULT 0`` would have rejected or coerced. This
   asserts against the migration's actual DDL.

2. **The ordering rule is enforced by the database, not by a read-then-write.**
   ``_UPSERT_FLOW``'s ``ON CONFLICT ... WHERE`` clause is what stops a replayed
   older window from overwriting a newer one. A read-compare-write in Python
   races under concurrent consumers; only the SQL predicate holds. It is tested
   against the real planner because a subtly wrong predicate silently degrades
   to "always update", which is indistinguishable from correct until a
   redelivery arrives.

Harness: a DISPOSABLE DATABASE (not a disposable schema) so this node's
migration applies BYTE-VERBATIM rather than through a rewrite that would weaken the evidence.
SKIPS (never ERRORs) without a reachable Postgres, mirroring
``tests/test_omn15909_real_postgres_projection_write_path_gate.py``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.nodes.node_projection_consumer_flow.handlers.handler_consumer_flow_runner import (
    _INSERT_UNKNOWN,
    _SELECT_PRIOR_STATE,
    _SELECT_UPSTREAM,
    _UPSERT_FLOW,
    _UPSERT_PRODUCE,
)
from omnimarket.nodes.node_projection_consumer_flow.models import (
    EnumConsumerFlowState,
    EnumUpstreamEvidence,
)

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_consumer_flow"
    / "migrations"
    / "0000_create_consumer_flow_windows.sql"
)

_T0 = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)
_GROUP = "onex-dev.omnimarket.gateway-link-health-projection-compute.consume"
_TOPIC = "onex.evt.platform.node-heartbeat.v1"  # onex-topic-allow: real topic from the OMN-16755 incident


def _dsn(database: str) -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{database}"


async def _connect_or_skip(database: str | None = None) -> asyncpg.Connection:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD / POSTGRES_PASSWORD not set — "
            "skipping the OMN-16777 real-Postgres write-path gate"
        )
    target = database or os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    try:
        return await asyncpg.connect(_dsn(target))
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for the OMN-16777 gate: {exc}")


@asynccontextmanager
async def _migrated_database() -> AsyncIterator[asyncpg.Connection]:
    """A throwaway database with this node's migration applied verbatim."""
    admin = await _connect_or_skip()
    name = f"omn16777_{uuid4().hex[:16]}"
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    except asyncpg.PostgresError as exc:  # pragma: no cover - infra
        await admin.close()
        pytest.skip(
            f"cannot create a disposable database for the OMN-16777 gate: {exc}"
        )
    conn: asyncpg.Connection | None = None
    try:
        conn = await asyncpg.connect(_dsn(name))
        # The node-owned migration loop connects to a database where
        # omninode_internal already exists; a throwaway database does not, so
        # the harness provides it rather than the migration (which deliberately
        # carries no CREATE SCHEMA — see its header, and OMN-16759).
        await conn.execute("CREATE SCHEMA IF NOT EXISTS omninode_internal")
        await conn.execute(_MIGRATION.read_text(encoding="utf-8"))
        yield conn
    finally:
        if conn is not None:
            await conn.close()
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await admin.close()


async def _insert_window(
    conn: asyncpg.Connection,
    *,
    sequence: int,
    start: datetime,
    end: datetime,
    node_id: str,
    messages_in: int,
    messages_out: int,
    upstream: int | None = None,
    state: EnumConsumerFlowState = EnumConsumerFlowState.FLOWING,
) -> list[asyncpg.Record]:
    return await conn.fetch(
        _UPSERT_FLOW,
        _GROUP,
        _TOPIC,
        start,
        end,
        node_id,
        sequence,
        messages_in,
        messages_out,
        0,
        0,
        upstream,
        EnumUpstreamEvidence.NONE.value,
        state.value,
        end,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_writers_own_sql_lands_a_correctly_typed_row() -> None:
    """The production statements, against the production schema.

    Every timestamp is bound as a real ``datetime``, never a string: the columns
    are ``TIMESTAMPTZ`` and asyncpg refuses the mismatch, which is the whole
    point of running this against Postgres rather than a double.
    """
    async with _migrated_database() as conn:
        node_id = str(uuid4())
        end = _T0 + timedelta(seconds=60)

        await conn.execute(_UPSERT_PRODUCE, _TOPIC, _T0, end, node_id, 1, 15750, end)
        upstream_rows = await conn.fetch(_SELECT_UPSTREAM, _TOPIC, _T0, end)
        assert upstream_rows[0]["window_count"] == 1
        assert upstream_rows[0]["produced"] == 15750

        rows = await _insert_window(
            conn,
            sequence=1,
            start=_T0,
            end=end,
            node_id=node_id,
            messages_in=15750,
            messages_out=0,
            upstream=15750,
            state=EnumConsumerFlowState.STALLED,
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["flow_state"] == EnumConsumerFlowState.STALLED.value
        assert row["messages_in"] == 15750
        assert row["messages_out"] == 0
        # Real column types, not whatever Python happened to hand over.
        assert isinstance(row["window_start"], datetime)
        assert row["window_start"].tzinfo is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unknown_row_stores_null_counters_because_the_columns_allow_it() -> None:
    """AC5 against the real DDL: ``UNKNOWN != 0 messages``.

    A ``NOT NULL DEFAULT 0`` on these columns would silently turn every dropped
    window into an idle one and no in-memory test could tell. This asserts the
    schema itself permits NULL, and that the gap statement actually writes it.
    """
    async with _migrated_database() as conn:
        node_id = str(uuid4())
        w1_end = _T0 + timedelta(seconds=60)
        w3_start = _T0 + timedelta(seconds=120)

        await _insert_window(
            conn,
            sequence=1,
            start=_T0,
            end=w1_end,
            node_id=node_id,
            messages_in=10,
            messages_out=10,
        )
        last = await conn.fetch(_SELECT_PRIOR_STATE, node_id)
        assert last[0]["last_sequence"] == 1

        gap = await conn.fetch(
            _INSERT_UNKNOWN,
            _GROUP,
            _TOPIC,
            w1_end,
            w3_start,
            node_id,
            2,
            EnumUpstreamEvidence.NONE.value,
            EnumConsumerFlowState.UNKNOWN.value,
            w3_start,
        )
        assert len(gap) == 1, "the gap statement wrote no row for the lost window"
        assert gap[0]["flow_state"] == EnumConsumerFlowState.UNKNOWN.value
        assert gap[0]["messages_in"] is None, (
            "the missed window stored 0 messages; a dropped heartbeat that reads "
            "as observed-idle is the exact false-green this ticket closes"
        )
        assert gap[0]["messages_out"] is None
        assert gap[0]["messages_dlq"] is None
        assert gap[0]["handler_errors"] is None

        nullable = {
            record["column_name"]: record["is_nullable"]
            for record in await conn.fetch(
                """
                SELECT column_name, is_nullable
                FROM information_schema.columns
                WHERE table_schema = 'omninode_internal'
                  AND table_name = 'consumer_flow_windows'
                """
            )
        }
        for column in (
            "messages_in",
            "messages_out",
            "messages_dlq",
            "handler_errors",
            "upstream_produced",
        ):
            assert nullable[column] == "YES", (
                f"{column} is NOT NULL in the real schema, so UNKNOWN cannot be "
                "distinguished from zero traffic"
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_replayed_older_window_is_refused_by_the_conflict_predicate() -> None:
    """Ordering is enforced by the database, not by a racy read-then-write.

    At-least-once delivery makes redelivery routine. If the ``ON CONFLICT ...
    WHERE`` predicate is wrong it degrades to "always update", which looks
    perfectly healthy right up until an old window replays over a new one.
    """
    async with _migrated_database() as conn:
        node_id = str(uuid4())
        end = _T0 + timedelta(seconds=60)

        await _insert_window(
            conn,
            sequence=9,
            start=_T0,
            end=end,
            node_id=node_id,
            messages_in=100,
            messages_out=100,
        )
        refused = await _insert_window(
            conn,
            sequence=8,
            start=_T0,
            end=end,
            node_id=node_id,
            messages_in=1,
            messages_out=0,
            state=EnumConsumerFlowState.STALLED,
        )
        assert refused == [], "an older window was allowed to overwrite a newer one"

        stored = await conn.fetchrow(
            """
            SELECT messages_in, ingest_sequence, flow_state
            FROM omninode_internal.consumer_flow_windows
            WHERE consumer_group = $1 AND topic = $2 AND window_start = $3
            """,
            _GROUP,
            _TOPIC,
            _T0,
        )
        assert stored is not None
        assert stored["messages_in"] == 100
        assert stored["ingest_sequence"] == 9
        assert stored["flow_state"] == EnumConsumerFlowState.FLOWING.value

        # And the same-sequence redelivery IS accepted — idempotent replay must
        # still work, or an at-least-once bus would wedge the projection.
        replayed = await _insert_window(
            conn,
            sequence=9,
            start=_T0,
            end=end,
            node_id=node_id,
            messages_in=100,
            messages_out=100,
        )
        assert len(replayed) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upstream_lookup_returns_no_windows_rather_than_zero() -> None:
    """``None`` and ``0`` are different answers and the SQL must keep them apart.

    ``window_count == 0`` means "nothing in this runtime publishes to that
    topic" — an externally-fed leg. Collapsing it to a produced total of 0 would
    let the derivation call a quiet external topic STARVED on no evidence, which
    is the alert storm AC4 forbids.
    """
    async with _migrated_database() as conn:
        rows = await conn.fetch(
            _SELECT_UPSTREAM,
            "onex.evt.external.never-published-here.v1",  # onex-topic-allow: synthetic externally-fed topic
            _T0,
            _T0 + timedelta(seconds=60),
        )
        assert rows[0]["window_count"] == 0
        assert rows[0]["produced"] == 0
