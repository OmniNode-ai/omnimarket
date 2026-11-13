# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Projection writer for the goal trace (OMN-20034, GC.11).

The effect-class half of the rule-7a pair. The runtime calls it once per
consumed message. It holds no rule of its own: the join, the order, the verdict
and the checksum are the fold's. What it adds is the persistence the fold cannot
do: store the event, load the scope the event can change, fold it, and write
back only the rows whose attachment moved.

Idempotency, by construction:

* an event row is keyed on (event id, body digest). A redelivery inserts nothing;
  the same id with another digest inserts a second row and the fold reports the
  conflict;
* every derived column (goal, position, assignment, conflict flag) is recomputed
  from the stored events of the scope and written only where it differs, so the
  result is the fold of the SET stored and not of the order the messages came;
* the per-goal row is an upsert of the fold's trace, never an increment.

The scope read grows to a fixpoint: the session's rows, then every row of each
goal those rows reach, until no new goal appears. A binding that arrives after
the hooks of its agent therefore re-attaches them, and a claim that arrives
after the binding is folded with it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from omnimarket.handlers.writer_pool_host import (
    WriterPoolHost,
    build_pool_adapter,
    load_writer_pool_settings,
)
from omnimarket.models.model_projection_request import ModelProjectionRequest
from omnimarket.models.model_writer_pool_settings import ModelWriterPoolSettings
from omnimarket.nodes.node_projection_goal_trace.handlers.handler_goal_trace_adapters import (
    MAIN_THREAD_AGENT,
    to_trace_event,
)
from omnimarket.nodes.node_projection_goal_trace.handlers.handler_projection_goal_trace import (
    HandlerProjectionGoalTrace,
)
from omnimarket.nodes.node_projection_goal_trace.models import (
    EnumGoalTraceEventKind,
    ModelGoalTrace,
    ModelGoalTraceEvent,
    ModelGoalTraceEventRow,
    ModelGoalTraceFoldRequest,
)
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

TABLE_EVENTS = "omninode_internal.goal_trace_events"
TABLE_TRACES = "omninode_internal.goal_traces"
TABLE_SPANS = "omninode_internal.claude_agent_spans"

# The most rounds the scope read can take. Each round adds at least one goal or
# stops, and a session reaches a handful of goals; the bound is a guard against
# a loop, not a limit anything is expected to meet.
_MAX_SCOPE_ROUNDS = 8

_SELECT_PARENT_SPAN = f"""
    SELECT parent_agent_id, parent_resolution FROM {TABLE_SPANS}
    WHERE session_id = $1 AND agent_id = $2
    LIMIT 1
"""

_INSERT_EVENT = f"""
    INSERT INTO {TABLE_EVENTS} AS existing (
        event_id, body_digest, kind, raw_goal_id, parent_goal_id, session_id,
        agent_key, parent_agent_key, emitted_at, revision, replaces,
        completed_at, verdict, item_hash, ingested_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, NOW())
    ON CONFLICT (event_id, body_digest) DO UPDATE SET
        parent_agent_key = EXCLUDED.parent_agent_key,
        updated_at = NOW()
    WHERE existing.parent_agent_key IS NULL
      AND EXCLUDED.parent_agent_key IS NOT NULL
    RETURNING event_id, (xmax = 0) AS event_inserted
"""

# The rows the event can change: its session (an agent's binding reaches every
# event of the session) and every row of the goals already reached.
_SELECT_SCOPE = f"""
    SELECT event_id, body_digest, kind, raw_goal_id, parent_goal_id, session_id,
           agent_key, parent_agent_key, emitted_at, revision, replaces,
           completed_at, verdict, item_hash,
           goal_id AS stored_goal_id, seq AS stored_seq,
           assignment AS stored_assignment, id_conflict AS stored_id_conflict
    FROM {TABLE_EVENTS}
    WHERE ($1::text IS NOT NULL AND session_id = $1)
       OR goal_id = ANY($2::uuid[])
       OR raw_goal_id = ANY($2::uuid[])
"""

_UPDATE_ROW = f"""
    UPDATE {TABLE_EVENTS}
    SET goal_id = $3, seq = $4, assignment = $5, id_conflict = $6,
        updated_at = NOW()
    WHERE event_id = $1 AND body_digest = $2
    RETURNING event_id
"""

_UPSERT_TRACE = f"""
    INSERT INTO {TABLE_TRACES} AS t (
        goal_id, parent_goal_ids, revisions, revision_edges, forked_revisions,
        closed_revision, competing_closes, attempt_ids, current_attempt_id,
        verdict, delegation_ids, receipt_ids, assigned_event_count,
        conflicting_event_ids, checksum, updated_at
    )
    VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, NOW())
    ON CONFLICT (goal_id) DO UPDATE SET
        parent_goal_ids = EXCLUDED.parent_goal_ids,
        revisions = EXCLUDED.revisions,
        revision_edges = EXCLUDED.revision_edges,
        forked_revisions = EXCLUDED.forked_revisions,
        closed_revision = EXCLUDED.closed_revision,
        competing_closes = EXCLUDED.competing_closes,
        attempt_ids = EXCLUDED.attempt_ids,
        current_attempt_id = EXCLUDED.current_attempt_id,
        verdict = EXCLUDED.verdict,
        delegation_ids = EXCLUDED.delegation_ids,
        receipt_ids = EXCLUDED.receipt_ids,
        assigned_event_count = EXCLUDED.assigned_event_count,
        conflicting_event_ids = EXCLUDED.conflicting_event_ids,
        checksum = EXCLUDED.checksum,
        updated_at = NOW()
    RETURNING goal_id
"""


def _event_params(
    event: ModelGoalTraceEvent, parent_agent_key: str | None
) -> tuple[Any, ...]:
    return (
        event.event_id,
        event.body_digest,
        event.kind.value,
        event.goal_id,
        event.parent_goal_id,
        event.session_id,
        event.agent_key,
        parent_agent_key,
        event.emitted_at,
        event.revision,
        event.replaces,
        event.completed_at,
        event.verdict,
        event.item_hash,
    )


def _event_from_record(record: dict[str, Any]) -> ModelGoalTraceEvent:
    return ModelGoalTraceEvent(
        event_id=record["event_id"],
        kind=EnumGoalTraceEventKind(record["kind"]),
        body_digest=record["body_digest"],
        emitted_at=record["emitted_at"],
        goal_id=record["raw_goal_id"],
        parent_goal_id=record["parent_goal_id"],
        session_id=record["session_id"],
        agent_key=record["agent_key"],
        parent_agent_key=record["parent_agent_key"],
        revision=record["revision"],
        replaces=record["replaces"],
        completed_at=record["completed_at"],
        verdict=record["verdict"],
        item_hash=record["item_hash"],
    )


def _row_params(row: ModelGoalTraceEventRow) -> tuple[Any, ...]:
    return (
        row.event_id,
        row.body_digest,
        row.goal_id,
        row.seq,
        row.assignment.value,
        row.id_conflict,
    )


def _trace_params(trace: ModelGoalTrace) -> tuple[Any, ...]:
    return (
        trace.goal_id,
        list(trace.parent_goal_ids),
        list(trace.revisions),
        json.dumps([list(edge) for edge in trace.revision_edges]),
        list(trace.forked_revisions),
        trace.closed_revision,
        list(trace.competing_closes),
        list(trace.attempt_ids),
        trace.current_attempt_id,
        trace.verdict,
        list(trace.delegation_ids),
        list(trace.receipt_ids),
        trace.assigned_event_count,
        list(trace.conflicting_event_ids),
        trace.checksum,
    )


def _moved(row: ModelGoalTraceEventRow, record: dict[str, Any]) -> bool:
    """True when the fold attached the stored row differently than it stands."""
    return (
        record["stored_goal_id"] != row.goal_id
        or record["stored_seq"] != row.seq
        or record["stored_assignment"] != row.assignment.value
        or bool(record["stored_id_conflict"]) != row.id_conflict
    )


class GoalTraceProjectionWriter(BaseProjectionRunner):
    """Projects hook events, goal-scoped verification results and trace events.

    ``Writer``, not ``Runner``: the type-word ratchet refuses ``Runner`` in a
    class name.
    """

    #: Dispatched IN-PROCESS by the runtime auto-wiring, once per consumed
    #: message. Undeclared, the shared runtime treats this as a standalone runner
    #: and never dispatches it: zero rows, no error (pinned by a test).
    onex_runtime_inprocess_dispatch = True

    def __init__(
        self,
        contract_path: Path | None = None,
        pool_settings: ModelWriterPoolSettings | None = None,
    ) -> None:
        super().__init__()
        _path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(_path) as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)
        self._fold = HandlerProjectionGoalTrace()
        self._pool_host = WriterPoolHost(
            "goal_trace_writer",
            lambda: build_pool_adapter(self.db, self._pool_host.settings),
            pool_settings or load_writer_pool_settings("node_projection_goal_trace"),
        )

    def start_pool(self) -> None:
        self._pool_host.start()

    def close_pool(self) -> None:
        self._pool_host.close()

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("subscribe_topics", []))

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    @property
    def poison_dlq_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("dlq_topics", []))

    def project_request(self, request: ModelProjectionRequest) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim: one message on the writer's one pool."""
        topics = self.subscribe_topics
        topic = request.topic_or(topics[0] if topics else "")
        payload = request.payload
        return self._pool_host.run(lambda db: self._project(db, topic, payload))

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        """Standalone-runner entrypoint: project one message, report success."""
        await self._project(self.db, topic, data)
        return True

    async def _parent_agent_key(
        self, db: Any, event: ModelGoalTraceEvent
    ) -> str | None:
        """The parent agent of a hook event's agent, if the span table knows it."""
        if (
            event.kind is not EnumGoalTraceEventKind.HOOK
            or event.session_id is None
            or event.agent_key is None
        ):
            return None
        agent_id = event.agent_key.removeprefix(f"{event.session_id}:")
        if agent_id == MAIN_THREAD_AGENT:
            return None
        found = await db.execute(_SELECT_PARENT_SPAN, event.session_id, agent_id)
        if not found:
            return None
        span = found[0]
        if span["parent_resolution"] == "main_thread":
            return f"{event.session_id}:{MAIN_THREAD_AGENT}"
        if span["parent_resolution"] == "resolved" and span["parent_agent_id"]:
            return f"{event.session_id}:{span['parent_agent_id']}"
        return None

    async def _load_scope(
        self, db: Any, session_id: str | None, goals: set[UUID]
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """The stored rows the new event can change, grown to a fixpoint."""
        reached: set[UUID] = set(goals)
        rows: dict[tuple[str, str], dict[str, Any]] = {}
        for _ in range(_MAX_SCOPE_ROUNDS):
            found = await db.execute(
                _SELECT_SCOPE, session_id, sorted(reached, key=str)
            )
            rows = {(r["event_id"], r["body_digest"]): dict(r) for r in found}
            discovered = {
                g
                for r in rows.values()
                for g in (r["raw_goal_id"], r["stored_goal_id"])
                if g is not None
            }
            if discovered <= reached:
                break
            reached |= discovered
        return rows

    async def _project(
        self, db: Any, topic: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        # A payload that is none of the three sources raises here, before any
        # write. The runtime classifies that as POISON and routes the raw
        # message to the contract's DLQ; it is never logged and dropped.
        event = to_trace_event(data)
        if event is None:
            return {
                "rows_upserted": 0,
                "event_inserted": False,
                "skipped": "not goal-scoped",
                "topic": topic,
            }

        parent_key = await self._parent_agent_key(db, event)
        inserted = await db.execute(_INSERT_EVENT, *_event_params(event, parent_key))
        event_inserted = bool(inserted and inserted[0].get("event_inserted", True))

        goals = {event.goal_id} if event.goal_id is not None else set()
        scope = await self._load_scope(db, event.session_id, goals)
        result = self._fold.handle(
            ModelGoalTraceFoldRequest(
                events=tuple(_event_from_record(r) for r in scope.values())
            )
        )

        rows = (
            [row for trace in result.traces for row in trace.events]
            + list(result.unassigned)
            + list(result.ambiguous)
        )
        rewritten = 0
        for row in rows:
            record = scope.get((row.event_id, row.body_digest))
            if record is None or _moved(row, record):
                rewritten += len(await db.execute(_UPDATE_ROW, *_row_params(row)))
        traces_written = 0
        for trace in result.traces:
            traces_written += len(
                await db.execute(_UPSERT_TRACE, *_trace_params(trace))
            )

        # rows_upserted is the rows this message put in the tables: the event it
        # carried, if new, plus every row it re-attached, plus the goal rows.
        rows_upserted = int(event_inserted) + rewritten + traces_written
        return {
            "rows_upserted": rows_upserted,
            "event_inserted": event_inserted,
            "event_id": event.event_id,
            "kind": event.kind.value,
            "goal_id": str(event.goal_id) if event.goal_id else None,
            "rewritten_rows": rewritten,
            "goal_rows_written": traces_written,
            "scope_rows": len(scope),
            "unassigned_count": result.unassigned_count,
            "ambiguous_count": result.ambiguous_count,
        }


__all__ = ["GoalTraceProjectionWriter"]
