# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Projection writer for captured Claude Code hook events (OMN-19513).

The effect-class half of the rule-7a pair. The runtime calls this, once per
consumed message. It resolves the one fact the fold cannot know -- which agent
made an earlier tool call -- by reading the table it owns, calls the fold, and
persists what the fold derived. It holds no rule of its own: the parent rule,
the span contribution and the out-of-order repair are all the fold's.

Both ways to get a projection writer wrong produce the SAME silent symptom
(every message consumed, offsets committed, zero rows, nothing raised): a pure
definition-B entry on the projection arm, and a runner-shaped class that does
not declare in-process dispatch. Both are pinned by tests.

Idempotency, by construction rather than by assumption:

* the event row is keyed on the producer's deterministic ``event_id`` and
  inserted ``ON CONFLICT DO NOTHING``, so a redelivery writes nothing new;
* the span is an upsert whose ``tool_call_count`` is RECOUNTED from the event
  table in the same statement, never incremented, so a redelivered
  ``PostToolUse`` cannot count twice;
* a known parent replaces an ``unknown`` one and is never replaced by one.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_projection_claude_hook_events.handlers.handler_projection_claude_hook_events import (
    HandlerProjectionClaudeHookEvents,
)
from omnimarket.nodes.node_projection_claude_hook_events.models import (
    ModelChildParentRepair,
    ModelClaudeAgentSpanUpdate,
    ModelClaudeHookEventRow,
    ModelClaudeHookEventWire,
    ModelClaudeHookProjectionRequest,
    ModelKnownParentToolCall,
)
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

TABLE_EVENTS = "omninode_internal.claude_hook_events"
TABLE_SPANS = "omninode_internal.claude_agent_spans"

# The spawning call, if it has been written. A tool_use_id is unique within a
# session, so one row at most can match; LIMIT 1 states that rather than
# relying on it.
_SELECT_PARENT_CALL = f"""
    SELECT agent_id FROM {TABLE_EVENTS}
    WHERE session_id = $1
      AND tool_use_id = $2
      AND hook_event_name = 'PreToolUse'
    LIMIT 1
"""

_INSERT_EVENT = f"""
    INSERT INTO {TABLE_EVENTS} (
        event_id, session_id, agent_id, is_subagent, agent_type,
        parent_tool_use_id, parent_agent_id, workflow_run_id, spawn_depth,
        hook_event_name, tool_use_id, tool_name, prompt_id, turn_id,
        correlation_id, causation_id, emitted_at, payload, content_ref_ids,
        source_topic, ingested_at
    )
    VALUES (
        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
        $15, $16, $17, $18::jsonb, $19, $20, NOW()
    )
    ON CONFLICT (event_id) DO NOTHING
    RETURNING event_id, projection_cursor, ingested_at
"""

# Recount, never increment: the count is a function of the event rows, so
# replaying any event converges on the same number.
_UPSERT_SPAN = f"""
    INSERT INTO {TABLE_SPANS} AS s (
        session_id, agent_id, agent_type, parent_tool_use_id, parent_agent_id,
        parent_resolution, workflow_run_id, spawn_depth, started_at, stopped_at,
        tool_call_count, first_seen_at, updated_at
    )
    VALUES (
        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
        (
            SELECT count(*)::integer FROM {TABLE_EVENTS} e
            WHERE e.session_id = $1
              AND e.agent_id = $2
              AND e.hook_event_name IN ('PostToolUse', 'PostToolUseFailure')
        ),
        NOW(), NOW()
    )
    ON CONFLICT (session_id, agent_id) DO UPDATE SET
        agent_type = COALESCE(s.agent_type, EXCLUDED.agent_type),
        parent_tool_use_id = COALESCE(s.parent_tool_use_id, EXCLUDED.parent_tool_use_id),
        workflow_run_id = COALESCE(s.workflow_run_id, EXCLUDED.workflow_run_id),
        spawn_depth = COALESCE(s.spawn_depth, EXCLUDED.spawn_depth),
        parent_agent_id = CASE
            WHEN s.parent_resolution = 'unknown'
                 AND EXCLUDED.parent_resolution <> 'unknown'
            THEN EXCLUDED.parent_agent_id
            ELSE s.parent_agent_id
        END,
        parent_resolution = CASE
            WHEN s.parent_resolution = 'unknown'
                 AND EXCLUDED.parent_resolution <> 'unknown'
            THEN EXCLUDED.parent_resolution
            ELSE s.parent_resolution
        END,
        started_at = LEAST(s.started_at, EXCLUDED.started_at),
        stopped_at = GREATEST(s.stopped_at, EXCLUDED.stopped_at),
        tool_call_count = EXCLUDED.tool_call_count,
        updated_at = NOW()
    RETURNING session_id, agent_id, parent_resolution, tool_call_count,
              projection_cursor
"""

# The out-of-order repair: a child span written before its spawning call.
_REPAIR_CHILD_SPANS = f"""
    UPDATE {TABLE_SPANS}
    SET parent_agent_id = $3, parent_resolution = $4, updated_at = NOW()
    WHERE session_id = $1
      AND parent_tool_use_id = $2
      AND parent_resolution = 'unknown'
    RETURNING agent_id
"""

_REPAIR_CHILD_EVENTS = f"""
    UPDATE {TABLE_EVENTS}
    SET parent_agent_id = $3
    WHERE session_id = $1
      AND parent_tool_use_id = $2
      AND parent_agent_id IS DISTINCT FROM $3
    RETURNING event_id
"""


def _event_params(row: ModelClaudeHookEventRow) -> tuple[Any, ...]:
    return (
        row.event_id,
        row.session_id,
        row.agent_id,
        row.is_subagent,
        row.agent_type,
        row.parent_tool_use_id,
        row.parent_agent_id,
        row.workflow_run_id,
        row.spawn_depth,
        row.hook_event_name.value,
        row.tool_use_id,
        row.tool_name,
        row.prompt_id,
        row.turn_id,
        row.correlation_id,
        row.causation_id,
        row.emitted_at,
        json.dumps(row.payload, sort_keys=True, separators=(",", ":")),
        list(row.content_ref_ids),
        row.source_topic,
    )


def _span_params(span: ModelClaudeAgentSpanUpdate) -> tuple[Any, ...]:
    return (
        span.session_id,
        span.agent_id,
        span.agent_type,
        span.parent_tool_use_id,
        span.parent_agent_id,
        span.parent_resolution.value,
        span.workflow_run_id,
        span.spawn_depth,
        span.seen_at,
        span.stopped_at,
    )


def _repair_params(repair: ModelChildParentRepair) -> tuple[Any, ...]:
    return (
        repair.session_id,
        repair.parent_tool_use_id,
        repair.parent_agent_id,
        repair.parent_resolution.value,
    )


class ClaudeHookEventsProjectionWriter(BaseProjectionRunner):
    """Projects ``onex.evt.omniclaude.hook-event.v1`` into the two tables.

    ``Writer``, not ``Runner``: the OMN-14350 type-word ratchet hard-fails
    ``Runner`` in a class name and its allowlist may only shrink.
    """

    #: Dispatched IN-PROCESS by the runtime auto-wiring, once per consumed
    #: message. Declaring it is a promise that every loop-bound resource is
    #: opened and closed inside the loop ``handle()`` opens. Undeclared, the
    #: shared runtime treats this as a standalone runner and never dispatches
    #: it, and no dedicated writer Deployment exists for this node: zero rows,
    #: no error.
    onex_runtime_inprocess_dispatch = True

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        _path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(_path) as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)
        self._derive = HandlerProjectionClaudeHookEvents()

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("subscribe_topics", []))

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    @property
    def poison_dlq_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("dlq_topics", []))

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim: one message, one loop, one pool."""
        topics = self.subscribe_topics
        topic = str(input_data.pop("_topic", topics[0] if topics else ""))
        meta = MessageMeta(
            partition=int(input_data.pop("_partition", 0)),
            offset=int(input_data.pop("_offset", 0)),
            fallback_id=str(input_data.pop("_fallback_id", "")),
            topic=topic,
        )
        return asyncio.run(self._project_one_message(topic, input_data, meta))

    async def _project_one_message(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> dict[str, Any]:
        """Project one runtime-dispatched message and report what it wrote.

        The mapping carries a real row count, which the runtime's write-path
        guard reads: a redelivered main-thread event writes nothing and says
        so, rather than acknowledging a write that did not happen.
        """
        await self.db.connect()
        try:
            return await self._project(topic, data)
        finally:
            await self.db.close()

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        """Standalone-runner entrypoint: project one message, report success."""
        await self._project(topic, data)
        return True

    async def _project(self, topic: str, data: dict[str, Any]) -> dict[str, Any]:
        # A malformed event raises ValidationError here, before any write. The
        # runtime classifies that as POISON and routes the raw message to the
        # contract's DLQ; it is never logged and dropped.
        event = ModelClaudeHookEventWire.model_validate(data)

        known: tuple[ModelKnownParentToolCall, ...] = ()
        parent_tool_use_id = event.lineage.parent_tool_use_id
        if parent_tool_use_id is not None:
            found = await self.db.execute(
                _SELECT_PARENT_CALL, event.lineage.session_id, parent_tool_use_id
            )
            known = tuple(
                ModelKnownParentToolCall(
                    session_id=event.lineage.session_id,
                    tool_use_id=parent_tool_use_id,
                    agent_id=row["agent_id"],
                )
                for row in found
            )

        result = self._derive.handle(
            ModelClaudeHookProjectionRequest(
                event=event,
                known_parent_calls=known,
                source_topic=topic or None,
            )
        )

        inserted = await self.db.execute(
            _INSERT_EVENT, *_event_params(result.event_row)
        )
        rows_upserted = len(inserted)

        span_row: dict[str, Any] | None = None
        if result.span_update is not None:
            returned = await self.db.execute(
                _UPSERT_SPAN, *_span_params(result.span_update)
            )
            rows_upserted += len(returned)
            span_row = dict(returned[0]) if returned else None

        repaired_spans = 0
        if result.resolves_children is not None:
            params = _repair_params(result.resolves_children)
            spans = await self.db.execute(_REPAIR_CHILD_SPANS, *params)
            events = await self.db.execute(_REPAIR_CHILD_EVENTS, *params[:3])
            repaired_spans = len(spans)
            rows_upserted += len(spans) + len(events)

        row = result.event_row
        return {
            "rows_upserted": rows_upserted,
            "event_inserted": bool(inserted),
            "event_id": str(row.event_id),
            "session_id": row.session_id,
            "agent_id": row.agent_id,
            "hook_event_name": row.hook_event_name.value,
            "parent_resolution": (
                span_row["parent_resolution"] if span_row is not None else None
            ),
            "repaired_child_spans": repaired_spans,
        }


__all__ = ["ClaudeHookEventsProjectionWriter"]
