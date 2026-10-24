# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21334: real committed projections close the compaction session barrier.

The DSN comes from ONEX_TEST_SESSION_BARRIER_DSN, or else from the shared
integration PostgreSQL fixture. Each test applies the projection migrations
in its own schema and removes that schema.
The writer SQL and folds are production code; the probe opens its normal
read-only connection rather than a database double.
"""

from __future__ import annotations

import importlib
import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from omnimarket.handlers.session_stream_barrier import HandlerSessionStreamBarrier
from omnimarket.nodes.node_capture_item_identity_effect.handlers.handler_item_identity import (
    build_barrier_payload,
)
from omnimarket.nodes.node_context_compaction_orchestrator.handlers.session_barrier_probe import (
    ModelSessionBarrierReadBinding,
    PostgresSessionBarrierProbeSource,
)
from omnimarket.nodes.node_projection_claude_hook_events.handlers import (
    handler_claude_hook_events_writer as lineage_writer,
)
from omnimarket.nodes.node_projection_claude_hook_events.handlers.handler_projection_claude_hook_events import (
    HandlerProjectionClaudeHookEvents,
)
from omnimarket.nodes.node_projection_claude_hook_events.models import (
    ModelClaudeHookEventWire,
    ModelClaudeHookProjectionRequest,
)
from omnimarket.nodes.node_projection_session_content.handlers import (
    handler_session_content as content_writer,
)
from omnimarket.nodes.node_projection_session_content.handlers.session_content_fold import (
    fold_session_content,
)
from omnimarket.nodes.node_projection_session_content.models.model_session_content import (
    ModelSessionContentRecord,
)
from omnimarket.projection.postgres_read_database import connect_read_only

psycopg2: Any = importlib.import_module("psycopg2")
pgextras: Any = importlib.import_module("psycopg2.extras")
sql: Any = importlib.import_module("psycopg2.sql")

pytestmark = pytest.mark.integration
_DSN_ENV = "ONEX_TEST_SESSION_BARRIER_DSN"
_ROOT = Path(__file__).resolve().parents[3]
_NODES = _ROOT / "src/omnimarket/nodes"
_CUTOFF = "2026-10-23T13:37:41Z"


@dataclass
class _ProjectedSession:
    conn: Any
    schema: str
    session_id: str
    source: PostgresSessionBarrierProbeSource

    def _write(
        self, query: str, params: tuple[Any, ...] | list[Any]
    ) -> list[dict[str, Any]]:
        # The production statements use asyncpg's positional placeholders. Both
        # INSERTs reference each parameter exactly once, in parameter order.
        query = re.sub(r"\$\d+", "%s", query).replace(
            "omninode_internal.", f"{self.schema}."
        )
        with self.conn.cursor() as cursor:
            cursor.execute(query, params)
            if cursor.description is None:
                return []
            columns = [column.name for column in cursor.description]
            return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def lineage(
        self,
        sequence: int,
        *,
        session_id: str | None = None,
        pre_upgrade: bool = False,
    ) -> list[dict[str, Any]]:
        session_id = session_id or self.session_id
        event = json.loads(
            (
                _ROOT / "tests/fixtures/claude_hook_capture/events/SessionStart.json"
            ).read_text(encoding="utf-8")
        )
        event["event_id"] = str(
            uuid5(NAMESPACE_URL, f"{session_id}:lineage:{sequence}")
        )
        event["lineage"]["session_id"] = session_id
        event["stream"] = "lineage"
        event["producer_seq"] = sequence
        row = (
            HandlerProjectionClaudeHookEvents()
            .handle(
                ModelClaudeHookProjectionRequest(
                    event=ModelClaudeHookEventWire.model_validate(event)
                )
            )
            .event_row
        )
        if pre_upgrade:
            row = row.model_copy(update={"producer_seq": None})
        return self._write(
            lineage_writer._INSERT_EVENT, lineage_writer._event_params(row)
        )

    def content(
        self,
        sequence: int,
        *,
        session_id: str | None = None,
        pre_upgrade: bool = False,
    ) -> None:
        session_id = session_id or self.session_id
        self.content_record(
            {
                "session_id": session_id,
                "content_record_id": f"{session_id}:content:{sequence}",
                "content_kind": "prompt",
                "content": "recorded content",
                "emitted_at": _CUTOFF,
                "stream": "content",
                "producer_seq": sequence,
            },
            pre_upgrade=pre_upgrade,
        )

    def content_record(
        self, record: dict[str, Any], *, pre_upgrade: bool = False
    ) -> None:
        row = fold_session_content(ModelSessionContentRecord.model_validate(record))
        if pre_upgrade:
            # The old fold retained stamped identity, but discarded these columns.
            row = row.model_copy(
                update={
                    "content_record_id": None,
                    "stream": None,
                    "producer_seq": None,
                    "record_type": None,
                    "barrier_id": None,
                    "high_water": None,
                    "content_capture": None,
                }
            )
        self._write(content_writer._UPSERT, content_writer._parameters(row))

    def barrier(
        self,
        stream: str,
        high_water: int,
        *,
        capture_enabled: bool = True,
        emitted_at: str = _CUTOFF,
        pre_upgrade: bool = False,
    ) -> None:
        self.content_record(
            build_barrier_payload(
                session_id=self.session_id,
                barrier_id="compaction-barrier",
                stream=stream,
                high_water_value=high_water,
                content_capture_enabled=capture_enabled,
                emitted_at=emitted_at,
            ),
            pre_upgrade=pre_upgrade,
        )


@pytest.fixture
def projected_session(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[_ProjectedSession]:
    dsn = os.environ.get(_DSN_ENV) or request.getfixturevalue(
        "integration_postgres_dsn"
    )
    monkeypatch.setenv(_DSN_ENV, dsn)
    conn = psycopg2.connect(dsn, connect_timeout=3)
    conn.autocommit = True
    pgextras.register_uuid(conn_or_curs=conn)
    schema = f"compaction_barrier_{uuid4().hex[:16]}"
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            cursor.execute("SELECT current_user")
            role = cursor.fetchone()[0]
            for node in (
                "node_projection_claude_hook_events",
                "node_projection_session_content",
            ):
                for migration in sorted((_NODES / node / "migrations").glob("*.sql")):
                    ddl = (
                        migration.read_text(encoding="utf-8")
                        .replace("omninode_internal", schema)
                        .replace("omninode_runtime", role)
                    )
                    cursor.execute(ddl)
        source = PostgresSessionBarrierProbeSource(
            ModelSessionBarrierReadBinding(
                dsn_env=_DSN_ENV,
                lineage_relation=f"{schema}.claude_hook_events",
                content_relation=f"{schema}.session_content",
            ),
            connect=connect_read_only,
            sql_composer=sql,
        )
        yield _ProjectedSession(conn, schema, str(uuid4()), source)
    finally:
        with conn.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema)
                )
            )
        conn.close()


def test_postgres_compaction_pack_build_barrier_waits_for_both_projection_gaps(
    projected_session: _ProjectedSession,
) -> None:
    projected = projected_session
    for sequence in (1, 3):
        projected.lineage(sequence)
        projected.content(sequence)
    # Another session's missing number cannot close this session's barrier.
    other_session = str(uuid4())
    projected.lineage(2, session_id=other_session)
    projected.content(2, session_id=other_session)
    projected.barrier("lineage", 3)
    projected.barrier("content", 3)
    probe = projected.source.read(projected.session_id, "compaction-barrier")
    assert probe.source_revision == _CUTOFF
    assert {barrier.stream: barrier.high_water for barrier in probe.barriers} == {
        "lineage": 3,
        "content": 3,
    }
    verdict = HandlerSessionStreamBarrier().handle(probe)
    assert verdict.outcome == "not_met"
    assert {state.stream: state.first_missing for state in verdict.streams} == {
        "lineage": 2,
        "content": 2,
    }

    projected.lineage(2)
    pending = HandlerSessionStreamBarrier().handle(
        projected.source.read(projected.session_id, "compaction-barrier")
    )
    assert pending.outcome == "not_met"
    assert (
        next(
            state for state in pending.streams if state.stream == "content"
        ).first_missing
        == 2
    )
    projected.content(2)
    closed = projected.source.read(projected.session_id, "compaction-barrier")
    assert HandlerSessionStreamBarrier().handle(closed).outcome == "met"
    assert all(
        sorted(closed.applied[stream]) == [1, 2, 3] for stream in ("lineage", "content")
    )


def test_postgres_compaction_pack_build_barrier_redelivery_is_idempotent(
    projected_session: _ProjectedSession,
) -> None:
    projected = projected_session
    for _ in range(2):
        projected.lineage(1)
        projected.content(1)
        projected.barrier("lineage", 1)
        projected.barrier("content", 1)
    probe = projected.source.read(projected.session_id, "compaction-barrier")
    assert probe.applied == {"lineage": (1,), "content": (1,)}
    assert len(probe.barriers) == 2
    assert HandlerSessionStreamBarrier().handle(probe).outcome == "met"
    # A barrier identity from a different compaction cannot be substituted.
    wrong_barrier = projected.source.read(projected.session_id, "other-barrier")
    assert wrong_barrier.barriers == ()
    assert wrong_barrier.source_revision is None
    assert HandlerSessionStreamBarrier().handle(wrong_barrier).outcome == "no_barrier"


def test_postgres_compaction_pack_build_barrier_preserves_disabled_and_utc_revision(
    projected_session: _ProjectedSession,
) -> None:
    projected = projected_session
    projected.lineage(1)
    projected.barrier(
        "lineage", 1, capture_enabled=False, emitted_at="2026-10-23T16:37:41+03:00"
    )
    probe = projected.source.read(projected.session_id, "compaction-barrier")
    assert probe.source_revision == _CUTOFF
    assert probe.applied == {"lineage": (1,), "content": ()}
    assert len(probe.barriers) == 1
    assert probe.barriers[0].content_capture == "disabled"
    verdict = HandlerSessionStreamBarrier().handle(probe)
    assert verdict.outcome == "disabled"
    assert verdict.streams[0].met


def test_postgres_compaction_pack_build_barrier_replay_backfills_old_projection_stamps(
    projected_session: _ProjectedSession,
) -> None:
    projected = projected_session
    inserted = projected.lineage(1, pre_upgrade=True)
    assert inserted[0]["event_inserted"] is True
    projected.content(1, pre_upgrade=True)
    projected.barrier("lineage", 1, pre_upgrade=True)
    projected.barrier("content", 1, pre_upgrade=True)
    before = projected.source.read(projected.session_id, "compaction-barrier")
    assert before.applied == {"lineage": (), "content": ()}
    assert before.barriers == ()

    # The same event ids now fill the formerly discarded nullable columns.
    backfilled = projected.lineage(1)
    assert backfilled[0]["event_id"] == inserted[0]["event_id"]
    assert backfilled[0]["event_inserted"] is False
    assert projected.lineage(1) == []  # Ordinary duplicate remains a no-op.
    projected.content(1)
    projected.barrier("lineage", 1)
    projected.barrier("content", 1)
    after = projected.source.read(projected.session_id, "compaction-barrier")
    assert after.applied == {"lineage": (1,), "content": (1,)}
    assert after.source_revision == _CUTOFF
    assert HandlerSessionStreamBarrier().handle(after).outcome == "met"
    with projected.conn.cursor() as cursor:
        cursor.execute(
            sql.SQL("SELECT count(*) FROM {}.session_content").format(
                sql.Identifier(projected.schema)
            )
        )
        assert cursor.fetchone()[0] == 3
