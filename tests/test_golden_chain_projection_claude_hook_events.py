# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19513 golden chain: the claude-hook-events projection as the runtime wires it.

Pins the two traps that make a projection write nothing without raising, and
the contract facts the runtime reads to wire this node at all.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_projection_claude_hook_events.handlers.handler_claude_hook_events_writer import (
    ClaudeHookEventsProjectionWriter,
)
from omnimarket.nodes.node_projection_claude_hook_events.handlers.handler_projection_claude_hook_events import (
    HandlerProjectionClaudeHookEvents,
)
from omnimarket.nodes.node_projection_claude_hook_events.models import (
    ModelClaudeHookProjectionRequest,
    ModelClaudeHookProjectionResult,
)

pytestmark = pytest.mark.unit

_NODE_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_claude_hook_events"
)
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "claude_hook_capture"

_SOURCE_TOPIC = "onex.evt.omniclaude.hook-event.v1"  # onex-topic-allow: the capture contract's metadata topic, omniclaude side of OMN-19513
_TERMINAL_TOPIC = "onex.evt.omnimarket.projection-claude-hook-events-applied.v1"  # onex-topic-allow: this node's declared terminal
_DLQ_TOPIC = "onex.dlq.omnimarket.projection-claude-hook-events-malformed.v1"  # onex-topic-allow: this node's declared DLQ


def _contract() -> dict[str, Any]:
    with open(_NODE_DIR / "contract.yaml") as handle:
        loaded: dict[str, Any] = yaml.safe_load(handle)
    return loaded


class _LoopBoundPool:
    """Reduced asyncpg: usable only from the loop that created it."""

    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()

    def check(self) -> None:
        if asyncio.get_running_loop() is not self.loop:
            raise RuntimeError("Event loop is closed")


class _RecordingAdapter:
    """Stands in for ``AsyncpgAdapter`` and enforces its loop affinity."""

    def __init__(self) -> None:
        self._pool: _LoopBoundPool | None = None
        self.statements: list[str] = []

    async def connect(self) -> None:
        self._pool = _LoopBoundPool()

    async def close(self) -> None:
        self._pool = None

    async def execute(self, query: str, *params: Any) -> list[dict[str, Any]]:
        assert self._pool is not None, "call connect() first"
        self._pool.check()
        self.statements.append(" ".join(query.split())[:40])
        if "RETURNING event_id, projection_cursor" in query:
            return [{"event_id": params[0], "projection_cursor": 1}]
        if "RETURNING session_id, agent_id" in query:
            return [
                {
                    "session_id": params[0],
                    "agent_id": params[1],
                    "parent_resolution": params[5],
                    "tool_call_count": 0,
                    "projection_cursor": 1,
                }
            ]
        return []


def _event(hook: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        (_FIXTURES / "events" / f"{hook}.json").read_text(encoding="utf-8")
    )
    return loaded


def test_the_contract_subscribes_the_one_capture_topic() -> None:
    contract = _contract()
    assert contract["event_bus"]["subscribe_topics"] == [_SOURCE_TOPIC]
    assert contract["terminal_event"] == _TERMINAL_TOPIC
    assert _TERMINAL_TOPIC in contract["event_bus"]["publish_topics"]
    assert contract["event_bus"]["dlq_topics"] == [_DLQ_TOPIC]


def test_the_contract_declares_both_tables_for_the_projection_arm() -> None:
    """Without db_io the consumer commits offsets and handle() never runs."""
    tables = _contract()["db_io"]["db_tables"]
    assert {(t["name"], t["schema"], t["access"]) for t in tables} == {
        ("claude_hook_events", "omninode_internal", "read_write"),
        ("claude_agent_spans", "omninode_internal", "read_write"),
    }


def test_the_node_attaches_on_lab_lanes_only() -> None:
    assert set(_contract()["runtime_lanes"]) == {
        "compose-dev",
        "onex-lab",
        "onex-lab-k3s",
    }


def test_the_writer_declares_in_process_dispatch() -> None:
    """Trap two: undeclared, the shared runtime skips it and writes zero rows."""
    assert ClaudeHookEventsProjectionWriter.onex_runtime_inprocess_dispatch is True


def test_the_routing_dispatches_the_writer_alone() -> None:
    """The fold is not a routed entry: the typed arm would unwrap its input.

    The wire event has a top-level ``payload`` mapping and an ``event_id``,
    which the runtime's typed arm reads as a transport envelope and unwraps
    through, handing a routed fold the per-hook payload without its lineage.
    """
    entries = _contract()["handler_routing"]["handlers"]
    assert [entry["handler"]["name"] for entry in entries] == [
        "ClaudeHookEventsProjectionWriter"
    ]
    assert _contract()["handler"]["class"] == "HandlerProjectionClaudeHookEvents"


def test_the_live_record_shape_is_envelope_shaped_to_the_runtime() -> None:
    """Pins the hazard the single-entry routing exists for.

    Mirrors the runtime's ``_is_transport_envelope`` predicate (a ``payload``
    mapping plus one marker key). If the capture contract renames its
    ``payload`` field, this fails, and the fold can be routed again.
    """
    markers = {
        "partition_key",
        "event_type",
        "envelope_id",
        "event_id",
        "correlation_id",
        "__debug_trace",
    }
    record = _event("PreToolUse")
    assert isinstance(record.get("payload"), dict)
    assert markers & record.keys()


def test_the_fold_accepts_the_bare_event_the_runtime_hands_it() -> None:
    """Trap one's mirror: the typed arm builds the request from a live message."""
    message = _event("SubagentStop")
    message["_topic"] = _SOURCE_TOPIC
    message["_envelope_id"] = "e-1"
    result = HandlerProjectionClaudeHookEvents().handle(
        ModelClaudeHookProjectionRequest.model_validate(message)
    )
    assert isinstance(result, ModelClaudeHookProjectionResult)
    assert result.span_update is not None
    assert result.span_update.stopped_at == result.event_row.emitted_at


def test_the_writer_entry_returns_a_row_count() -> None:
    """A written row cannot be reported as zero, nor a non-write as one."""
    writer = ClaudeHookEventsProjectionWriter()
    adapter = _RecordingAdapter()
    writer._db = adapter  # type: ignore[assignment]
    message = _event("PostToolUse")
    message["_topic"] = _SOURCE_TOPIC
    result = writer.handle(message)
    assert result["rows_upserted"] == 1
    assert result["event_inserted"] is True
    assert result["hook_event_name"] == "PostToolUse"


def test_the_emit_seams_transport_stamps_are_accepted() -> None:
    """A flat record as the emit daemon publishes it, stamps and all."""
    writer = ClaudeHookEventsProjectionWriter()
    writer._db = _RecordingAdapter()  # type: ignore[assignment]
    message = _event("Stop")
    message.update(
        {
            "correlation_id": "4f1c1f3e-7d0a-4d3b-9b5e-2f9e1c0a7b11",
            "causation_id": None,
            "entity_id": "0b8f7b0c-5d7e-4c35-8a8e-3c3a8f7a9d21",
            "session_id": "daemon-process-session",
            "redaction_state": "clean",
            "_topic": _SOURCE_TOPIC,
        }
    )
    result = writer.handle(message)
    assert result["event_inserted"] is True
    # The event's own lineage wins over the daemon's environment fallback.
    assert result["session_id"] == _event("Stop")["lineage"]["session_id"]


def test_two_consecutive_messages_do_not_share_a_loop_bound_pool() -> None:
    """One message cannot expose a pool cached across calls; two can."""
    writer = ClaudeHookEventsProjectionWriter()
    adapter = _RecordingAdapter()
    writer._db = adapter  # type: ignore[assignment]
    for hook in ("SubagentStart", "SubagentStop"):
        message = _event(hook)
        message["_topic"] = _SOURCE_TOPIC
        result = writer.handle(message)
        assert result["rows_upserted"] == 2
