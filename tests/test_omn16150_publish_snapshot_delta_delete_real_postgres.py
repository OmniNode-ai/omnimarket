# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16150: real-Postgres coherence proof for the projection delete/tombstone
key fix.

``BaseProjectionRunner.publish_snapshot_delta(op="delete")`` used to derive
its Kafka message key from the (necessarily ``None``) ``row`` argument,
raising ``KeyError`` on every call -- a delete could never succeed. The fix
(``src/omnimarket/projection/runner.py``) adds a dedicated ``key`` parameter
for the delete path, required for ``op="delete"`` and forbidden for
``op="upsert"`` (row stays the single key source there).

``tests/test_projection_runner.py::TestPublishSnapshotDeltaDelete`` already
proves the mock-DB-free unit contract (key present, value ``None``, both
op-mismatch guards). This module is a required companion under this repo's
projection-write-path real-DB gate (``scripts/ci/check_projection_write_path_db_gate.py``,
OMN-15909) because the diff touches ``src/omnimarket/projection/runner.py``.
It proves something a unit test cannot: that the delete path's key derivation
stays byte-coherent with the upsert path's key derivation for the SAME row
identity, once that identity has round-tripped through a REAL, migrated
Postgres table (``HandlerLiveEventsProjectionRunner``'s ``live_events``) --
not a hand-typed Python literal that happens to already be a ``str``. Skips
(never errors) without a reachable database, mirroring the established idiom
in ``tests/test_writer_tenant_isolation_omn14898.py`` and
``tests/test_omn15909_real_postgres_projection_write_path_gate.py``.

Only migration ``0000_create_live_events.sql`` is applied (not the full
node's migration set): ``0001`` is a one-time data-repair UPDATE against an
already-populated table (a no-op on an empty one) and ``0002`` creates a
DIFFERENT table in the cluster-wide ``omninode_internal`` schema that this
runner's write path never touches -- applying it would couple this test to
that schema's existence for no proof value.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_live_events.handlers.handler_live_events import (
    HandlerLiveEventsProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_live_events"
    / "migrations"
    / "0000_create_live_events.sql"
)

NODE_HEARTBEAT_TOPIC = "onex.evt.platform.node-heartbeat.v1"


class _RecordingProducer:
    """Fake ``AIOKafkaProducer`` capturing ``send_and_wait`` calls.

    The DB side of this test is real (real Postgres, real migrated schema,
    real ``AsyncpgAdapter``); the Kafka side is intentionally faked -- this
    gate's concern is column-type coherence on the write path, not broker
    connectivity, and injecting this directly as ``runner._producer`` (like
    the mock-DB unit tests in ``tests/test_projection_runner.py``) avoids
    depending on a live broker for a Postgres-focused proof.
    """

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_and_wait(
        self,
        topic: str,
        value: bytes | None = None,
        key: bytes | None = None,
        headers: list[tuple[str, bytes]] | None = None,
    ) -> None:
        self.sent.append(
            {"topic": topic, "value": value, "key": key, "headers": headers}
        )


def _base_dsn() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


async def _connect_or_skip() -> asyncpg.Connection:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip(
            "POSTGRES_PASSWORD not set -- skipping OMN-16150 real-Postgres "
            "delete-key coherence proof"
        )
    try:
        return await asyncpg.connect(_base_dsn())
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for OMN-16150 delete-key proof: {exc}")


@asynccontextmanager
async def _provisioned_runner() -> AsyncIterator[
    tuple[HandlerLiveEventsProjectionRunner, asyncpg.Connection, str]
]:
    """Provision a disposable schema with the live-events table, bind a real
    asyncpg-backed ``HandlerLiveEventsProjectionRunner`` to it, and yield
    ``(runner, admin_conn, schema)`` -- mirrors
    ``tests/test_omn15909_real_postgres_projection_write_path_gate.py::_provisioned_runner``.
    """
    admin_conn = await _connect_or_skip()
    schema = f"omn16150_{uuid4().hex[:16]}"
    pool: asyncpg.Pool | None = None
    try:
        await admin_conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin_conn.execute(f"CREATE SCHEMA {schema}")
        await admin_conn.execute(f"SET search_path TO {schema}, public")
        await admin_conn.execute(_MIGRATION.read_text(encoding="utf-8"))

        pool = await asyncpg.create_pool(
            _base_dsn(),
            min_size=1,
            max_size=3,
            server_settings={"search_path": f"{schema},public"},
        )
        adapter = AsyncpgAdapter(dsn=_base_dsn())
        adapter._pool = pool  # type: ignore[attr-defined]

        runner = HandlerLiveEventsProjectionRunner()
        runner._db = adapter  # type: ignore[assignment]

        yield runner, admin_conn, schema
    finally:
        if pool is not None:
            with contextlib.suppress(Exception):
                await pool.close()
        with contextlib.suppress(Exception):
            await admin_conn.execute("SET search_path TO public")
            await admin_conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin_conn.close()


@pytest.mark.integration
class TestDeleteKeyCoherenceAgainstRealPostgres:
    """The delete path's key derivation must match the upsert path's key
    derivation for the SAME real, Postgres-round-tripped row.
    """

    async def test_delete_key_matches_the_real_upserted_rows_key(self) -> None:
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            producer = _RecordingProducer()
            runner._producer = producer  # type: ignore[assignment]
            event_id = str(uuid4())
            meta = MessageMeta(
                partition=0,
                offset=0,
                fallback_id=event_id,
                topic=NODE_HEARTBEAT_TOPIC,
            )

            ok = await runner.project_event(
                NODE_HEARTBEAT_TOPIC,
                {"event_id": event_id, "summary": "omn16150 real-db proof"},
                meta,
            )
            assert ok is True
            assert len(producer.sent) == 1, (
                "the real upsert must publish exactly one snapshot delta"
            )
            upsert_sent = producer.sent[0]
            assert upsert_sent["value"] is not None

            row = await admin_conn.fetchrow(
                "SELECT event_id FROM live_events WHERE event_id = $1", event_id
            )
            assert row is not None, "the real Postgres UPSERT must have landed"
            real_event_id = row["event_id"]

            exposure = runner._snapshot_exposure
            assert exposure is not None

            published = await runner.publish_snapshot_delta(
                exposure,
                op="delete",
                row=None,
                key={"event_id": real_event_id},
                source_event_id=event_id,
                source_topic=NODE_HEARTBEAT_TOPIC,
                source_partition=0,
                source_offset=1,
            )

            assert published is True
            assert len(producer.sent) == 2
            delete_sent = producer.sent[1]
            assert delete_sent["value"] is None, (
                "a delete must publish a genuine tombstone (value=None)"
            )
            assert delete_sent["key"] == upsert_sent["key"], (
                "the delete path's key derivation must byte-match the upsert "
                "path's key derivation for the identical real "
                "Postgres-round-tripped identity -- the OMN-16150 "
                "contract-coherence guarantee"
            )
