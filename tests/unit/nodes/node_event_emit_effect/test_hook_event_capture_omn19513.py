# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""All-hooks capture: the hook.event registration and its redaction (OMN-19513).

The operator ruled "we need all hooks captured". omniclaude's producer journals
one ``hook.event`` per Claude Code hook call, carrying a nested ``lineage`` and
``payload`` object (the ``claude_hook_capture`` contract). Two things must hold
here, or the capture is either unpublishable or useless:

* the emit registry routes ``hook.event`` to its topic, partitioned by session;
* the redaction contract governs the nested members by dotted name, so the
  lineage crosses verbatim. Before this change an object field was one field:
  undeclared, it was hashed whole, and every lineage id reached the bus as a
  single ``sha256:`` digest.

Planted secrets are built by concatenation and carry ``FAKE``.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_event_emit_effect.redaction import (
    EnumCaptureClass,
    EnumRedactionState,
    load_contract,
    redact_capture,
)
from omnimarket.nodes.node_event_emit_effect.spool.topic_resolver import (
    resolve_event_type,
    resolve_partition_key_field,
)

HOOK_EVENT = "hook.event"
HOOK_TOPIC = "onex.evt.omniclaude.hook-event.v1"
TOOL_TOPIC = "onex.evt.omniclaude.tool-executed.v1"

_SHA = "a" * 64


def _event(**overrides: Any) -> dict[str, Any]:
    """A PostToolUse hook.event as omniclaude's producer journals it."""
    event: dict[str, Any] = {
        "event_id": "3f9a2c1e-0000-5000-8000-000000000001",
        "schema_version": "1.0.0",
        "hook_event_name": "PostToolUse",
        "emitted_at": "2026-09-26T12:00:00+00:00",
        "actor": "claude",
        "claude_code_version": "2.1.283",
        "lineage": {
            "session_id": "11111111-2222-3333-4444-555555555555",
            "agent_id": "a1b2c3d4e5f6a7b8c",
            "agent_type": "general-purpose",
            "is_subagent": True,
            "parent_tool_use_id": "toolu_parent",
            "workflow_run_id": None,
            "spawn_depth": 1,
            "tool_use_id": "toolu_child",
            "prompt_id": None,
            "turn_id": "11111111-2222-3333-4444-555555555555:turn-3",
            "correlation_id": "3f9a2c1e-0000-5000-8000-000000000002",
            "causation_id": "3f9a2c1e-0000-5000-8000-000000000003",
        },
        "payload": {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "duration_ms": 12,
            "interrupted": False,
            "tool_input_ref": {
                "field": "tool_input",
                "sha256": _SHA,
                "length": 10,
                "content_record_id": "3f9a2c1e-0000-5000-8000-000000000004",
            },
        },
        "content_refs": [
            {
                "field": "tool_input",
                "sha256": _SHA,
                "length": 10,
                "content_record_id": "3f9a2c1e-0000-5000-8000-000000000004",
            }
        ],
        "session_id": "11111111-2222-3333-4444-555555555555",
    }
    event.update(overrides)
    return event


@pytest.mark.unit
def test_hook_event_routes_to_its_topic_partitioned_by_session() -> None:
    targets = resolve_event_type(HOOK_EVENT)
    assert [(t.topic, t.transform_name) for t in targets] == [
        (HOOK_TOPIC, "redact_capture")
    ]
    assert resolve_partition_key_field(HOOK_EVENT) == "session_id"


@pytest.mark.unit
def test_lineage_crosses_verbatim_member_by_member() -> None:
    out = redact_capture(_event(), HOOK_TOPIC)
    assert out["lineage"] == _event()["lineage"]
    assert out["payload"]["tool_name"] == "Bash"
    assert out["payload"]["tool_input_ref"]["sha256"] == _SHA
    assert out["content_refs"] == _event()["content_refs"]
    assert out["session_id"] == _event()["session_id"]
    assert out["redaction_state"] == EnumRedactionState.REDACTED.value


@pytest.mark.unit
def test_an_undeclared_member_is_hashed_not_passed() -> None:
    event = _event()
    event["payload"] = {**event["payload"], "tool_input": "rm -rf /tmp/x"}
    event["lineage"] = {**event["lineage"], "surprise": "value"}
    out = redact_capture(event, HOOK_TOPIC)
    assert out["payload"]["tool_input"].startswith("sha256:")
    assert out["lineage"]["surprise"].startswith("sha256:")
    # The declared members beside them are untouched.
    assert out["lineage"]["agent_id"] == "a1b2c3d4e5f6a7b8c"


@pytest.mark.unit
def test_a_secret_in_a_verbatim_member_is_hashed_and_escalates() -> None:
    planted = "ghp" + "_" + ("FAKE" + "a1b2c3d4e5" * 4)[:36]
    event = _event()
    event["lineage"] = {**event["lineage"], "agent_type": planted}
    out = redact_capture(event, HOOK_TOPIC)
    assert out["lineage"]["agent_type"].startswith("sha256:")
    assert "FAKE" not in str(out)
    assert out["redaction_state"] == EnumRedactionState.SECRET_DETECTED.value


@pytest.mark.unit
def test_an_object_with_no_dotted_declarations_is_still_one_field() -> None:
    # The pre-OMN-19513 behaviour holds for every topic that declares no
    # dotted members: an undeclared object is hashed whole.
    out = redact_capture(
        {"session_id": "s", "tool_name": "Bash", "nested": {"a": 1}}, TOOL_TOPIC
    )
    assert isinstance(out["nested"], str)
    assert out["nested"].startswith("sha256:")


@pytest.mark.unit
def test_every_declared_hook_event_member_is_verbatim() -> None:
    policy = load_contract().topics[HOOK_TOPIC]
    dotted = [name for name in policy.fields if "." in name]
    assert any(name.startswith("lineage.") for name in dotted)
    assert any(name.startswith("payload.") for name in dotted)
    assert set(policy.fields.values()) == {EnumCaptureClass.CAPTURE_VERBATIM}


@pytest.mark.unit
def test_tool_executed_carries_the_tool_use_and_agent_ids_verbatim() -> None:
    out = redact_capture(
        {
            "session_id": "s",
            "tool_name": "Read",
            "tool_use_id": "toolu_01",
            "agent_id": "a1b2c3",
        },
        TOOL_TOPIC,
    )
    assert out["tool_use_id"] == "toolu_01"
    assert out["agent_id"] == "a1b2c3"


@pytest.mark.unit
def test_the_journal_and_drainer_stamps_cross_verbatim() -> None:
    # Every top-level name the hook journal and the drainer add to a record
    # must cross as written: the projection writer (omnimarket#2956) accepts
    # exactly these stamps and dead-letters a record carrying a digest in
    # their place. hook_fired_at was hashed by the default until declared.
    stamps = {
        "hook_fired_at": "2026-09-26T12:00:00.123456+00:00",
        "turn_id": "11111111-2222-3333-4444-555555555555:turn-3",
        "lane": "all-hooks-producers-83",
        "lane_source": "sidecar",
        "lane_ticket": "OMN-19513",
        "workspace_path": "omni_worktrees/OMN-19513/omniclaude",
        "correlation_id": "3f9a2c1e-0000-5000-8000-000000000002",
        "causation_id": "3f9a2c1e-0000-5000-8000-000000000003",
        "entity_id": "11111111-2222-3333-4444-555555555555",
    }
    out = redact_capture(_event(**stamps), HOOK_TOPIC)
    assert {name: out[name] for name in stamps} == stamps
