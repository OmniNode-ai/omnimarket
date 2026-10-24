# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure, explicit metadata projection; no content is copied from the trace."""

from datetime import datetime
from uuid import UUID, uuid5

from pydantic import JsonValue

from omnimarket.nodes.node_beacon_trace_capture_effect.models.model_beacon_hook_event import (
    ModelBeaconHookEvent,
    ModelBeaconHookLineage,
)

NAMESPACE = UUID("53bed8d7-b0d3-49eb-a75c-ae5335770bf4")


def _object(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _string(value: JsonValue) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _count(value: JsonValue) -> int | None:
    # In particular, never copy strings from nominally numeric attributes.
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def map_beacon_line(
    line: dict[str, JsonValue], *, lane: str | None, host: str | None
) -> ModelBeaconHookEvent | None:
    """Map a Codex runtime record using only the named metadata fields."""
    if _object(line.get("harness")).get("name") != "codex_cli":
        return None
    session_id = _string(_object(line.get("session")).get("id"))
    if session_id is None:
        return None
    event = _object(line.get("event"))
    action = event.get("action")
    raw = _object(line.get("raw"))
    attrs = _object(raw.get("attributes"))
    model = _string(line.get("model"))
    payload: dict[str, JsonValue] = {"source": "beacon"}
    if action == "session.started":
        name = "SessionStart"
        payload["model"] = model
    elif action == "prompt.submitted":
        name = "UserPromptSubmit"
        payload["prompt_length"] = _count(attrs.get("prompt_length"))
    elif action == "approval.requested":
        name = "PermissionRequest"
        payload["tool_name"] = _string(attrs.get("tool_name"))
    elif action in ("command.executed", "tool.invoked"):
        name = "PostToolUseFailure" if attrs.get("success") is False else "PostToolUse"
        payload["tool_name"] = _string(attrs.get("tool_name"))
        payload["duration_ms"] = _count(attrs.get("duration_ms"))
    elif action == "token.usage":
        if model is None:
            return None
        name = "AssistantUsage"
        payload.update(model=model, message_count=1, usage_source_hook="beacon")
        for target, source in (
            ("input_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
            ("cache_read_input_tokens", "cached_input_tokens"),
            ("cache_creation_input_tokens", "cache_write_input_tokens"),
        ):
            payload[target] = _count(attrs.get(f"codex.turn.token_usage.{source}")) or 0
    else:
        return None
    payload["hook_event_name"] = name

    event_id = _string(event.get("id"))
    timestamp = _string(line.get("timestamp"))
    if event_id is None or timestamp is None:
        return None
    try:
        UUID(event_id)
        emitted_at = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if emitted_at.utcoffset() is None:
        return None
    return ModelBeaconHookEvent(
        event_id=uuid5(NAMESPACE, f"beacon:{event_id}"),
        hook_event_name=name,
        emitted_at=emitted_at,
        host=host,
        lane=lane,
        lineage=ModelBeaconHookLineage(
            session_id=session_id,
            tool_use_id=_string(attrs.get("call_id")),
            turn_id=_string(raw.get("turn_id")),
            correlation_id=uuid5(NAMESPACE, f"beacon-correlation:{session_id}"),
        ),
        payload={key: value for key, value in payload.items() if value is not None},
    )
