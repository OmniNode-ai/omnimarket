# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19513: the claude-hook-events projection against the capture contract.

The fixtures under ``tests/fixtures/claude_hook_capture`` are copied verbatim
from the omniclaude capture-contract PR (omniclaude#2370, head 22c255f242),
where ``scripts/generate_claude_hook_capture_fixtures.py`` generates them:

* ``events/<Hook>.json`` -- one wire event per hook type, all 33;
* ``scenarios/*.events.jsonl`` -- a nested subagent tree, a Workflow-spawned
  agent and an orphan agent whose sidecar was unreadable;
* ``expected_projection/*.jsonl`` -- the rows the contract says this writer
  must reproduce from those scenarios;
* ``capture_contract_pins.json`` -- the facts this node must agree with, read
  out of the contract at that head: the 33 hook types, the metadata payload
  keys, and the projection columns. (The contract YAML itself is not copied:
  it names topics, which this repo only accepts from contracts it owns.)

The scenario tests drive the REAL writer entry (``handle()``, the one the
runtime calls) against an in-memory stand-in for the database that executes
the writer's own statements, identified by the module constants. The SQL
semantics themselves are proven against real Postgres in
``test_omn19513_claude_hook_events_real_postgres.py``.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_projection_claude_hook_events.handlers import (
    handler_claude_hook_events_writer as writer_module,
)
from omnimarket.nodes.node_projection_claude_hook_events.handlers.handler_claude_hook_events_writer import (
    ClaudeHookEventsProjectionWriter,
)
from omnimarket.nodes.node_projection_claude_hook_events.handlers.handler_projection_claude_hook_events import (
    SOURCE_TOPIC,
    HandlerProjectionClaudeHookEvents,
)
from omnimarket.nodes.node_projection_claude_hook_events.models import (
    ALLOWED_PAYLOAD_KEYS,
    EnumClaudeHookEventName,
    EnumParentResolution,
    ModelClaudeHookEventWire,
    ModelClaudeHookProjectionRequest,
    ModelKnownParentToolCall,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "claude_hook_capture"
SCENARIOS = ("subagent_tree", "workflow_agent", "orphan_agent")


#: The capture contract's metadata topic, as omniclaude#2370 declares it.
CAPTURE_TOPIC = "onex.evt.omniclaude.hook-event.v1"  # onex-topic-allow: the omniclaude capture contract's metadata topic


def _capture_contract() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        (FIXTURES / "capture_contract_pins.json").read_text(encoding="utf-8")
    )
    return loaded


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _scenario_events(name: str) -> list[dict[str, Any]]:
    return _jsonl(FIXTURES / "scenarios" / f"{name}.events.jsonl")


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


class _InMemoryHookTables:
    """Executes the writer's own statements against two in-memory tables.

    Statements are recognised by identity with the writer's module constants,
    so a statement the writer issues that this stand-in does not know fails
    loudly instead of being answered with an empty list.
    """

    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}
        self.spans: dict[tuple[str, str], dict[str, Any]] = {}
        self._cursor = 0
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    def _next_cursor(self) -> int:
        self._cursor += 1
        return self._cursor

    async def execute(self, query: str, *params: Any) -> list[dict[str, Any]]:
        assert self.connected, "call connect() first"
        if query is writer_module._SELECT_PARENT_CALL:
            session_id, tool_use_id = params
            return [
                {"agent_id": row["agent_id"]}
                for row in self.events.values()
                if row["session_id"] == session_id
                and row["tool_use_id"] == tool_use_id
                and row["hook_event_name"] == "PreToolUse"
            ][:1]
        if query is writer_module._INSERT_EVENT:
            return self._insert_event(params)
        if query is writer_module._UPSERT_SPAN:
            return self._upsert_span(params)
        if query is writer_module._REPAIR_CHILD_SPANS:
            session_id, parent_tool_use_id, parent_agent_id, resolution = params
            repaired = []
            for span in self.spans.values():
                if (
                    span["session_id"] == session_id
                    and span["parent_tool_use_id"] == parent_tool_use_id
                    and span["parent_resolution"] == "unknown"
                ):
                    span["parent_agent_id"] = parent_agent_id
                    span["parent_resolution"] = resolution
                    repaired.append({"agent_id": span["agent_id"]})
            return repaired
        if query is writer_module._REPAIR_CHILD_EVENTS:
            session_id, parent_tool_use_id, parent_agent_id = params
            repaired = []
            for row in self.events.values():
                if (
                    row["session_id"] == session_id
                    and row["parent_tool_use_id"] == parent_tool_use_id
                    and row["parent_agent_id"] != parent_agent_id
                ):
                    row["parent_agent_id"] = parent_agent_id
                    repaired.append({"event_id": row["event_id"]})
            return repaired
        raise AssertionError(f"unexpected statement: {query[:80]}")

    def _insert_event(self, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        columns = (
            "event_id",
            "session_id",
            "agent_id",
            "is_subagent",
            "agent_type",
            "parent_tool_use_id",
            "parent_agent_id",
            "workflow_run_id",
            "spawn_depth",
            "hook_event_name",
            "tool_use_id",
            "tool_name",
            "prompt_id",
            "turn_id",
            "correlation_id",
            "causation_id",
            "emitted_at",
            "payload",
            "content_ref_ids",
            "source_topic",
        )
        assert len(params) == len(columns)
        row = dict(zip(columns, params, strict=True))
        key = str(row["event_id"])
        if key in self.events:
            return []
        row["projection_cursor"] = self._next_cursor()
        self.events[key] = row
        return [
            {"event_id": row["event_id"], "projection_cursor": row["projection_cursor"]}
        ]

    def _upsert_span(self, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        (
            session_id,
            agent_id,
            agent_type,
            parent_tool_use_id,
            parent_agent_id,
            parent_resolution,
            workflow_run_id,
            spawn_depth,
            started_at,
            stopped_at,
        ) = params
        count = sum(
            1
            for row in self.events.values()
            if row["session_id"] == session_id
            and row["agent_id"] == agent_id
            and row["hook_event_name"] in {"PostToolUse", "PostToolUseFailure"}
        )
        key = (session_id, agent_id)
        span = self.spans.get(key)
        if span is None:
            span = {
                "session_id": session_id,
                "agent_id": agent_id,
                "agent_type": agent_type,
                "parent_tool_use_id": parent_tool_use_id,
                "parent_agent_id": parent_agent_id,
                "parent_resolution": parent_resolution,
                "workflow_run_id": workflow_run_id,
                "spawn_depth": spawn_depth,
                "started_at": started_at,
                "stopped_at": stopped_at,
                "tool_call_count": count,
                "projection_cursor": self._next_cursor(),
            }
            self.spans[key] = span
        else:
            upgrade = (
                span["parent_resolution"] == "unknown"
                and parent_resolution != "unknown"
            )
            span["agent_type"] = span["agent_type"] or agent_type
            span["parent_tool_use_id"] = (
                span["parent_tool_use_id"] or parent_tool_use_id
            )
            span["workflow_run_id"] = span["workflow_run_id"] or workflow_run_id
            if span["spawn_depth"] is None:
                span["spawn_depth"] = spawn_depth
            if upgrade:
                span["parent_agent_id"] = parent_agent_id
                span["parent_resolution"] = parent_resolution
            span["started_at"] = min(span["started_at"], started_at)
            stops = [s for s in (span["stopped_at"], stopped_at) if s is not None]
            span["stopped_at"] = max(stops) if stops else None
            span["tool_call_count"] = count
        return [
            {
                "session_id": session_id,
                "agent_id": agent_id,
                "parent_resolution": span["parent_resolution"],
                "tool_call_count": span["tool_call_count"],
                "projection_cursor": span["projection_cursor"],
            }
        ]

    def event_rows_as_contract_json(self) -> dict[str, dict[str, Any]]:
        """Stored event rows in the shape the contract's expected rows use."""
        out: dict[str, dict[str, Any]] = {}
        for key, row in self.events.items():
            out[key] = {
                "event_id": str(row["event_id"]),
                "session_id": row["session_id"],
                "agent_id": row["agent_id"],
                "is_subagent": row["is_subagent"],
                "agent_type": row["agent_type"],
                "parent_tool_use_id": row["parent_tool_use_id"],
                "parent_agent_id": row["parent_agent_id"],
                "workflow_run_id": row["workflow_run_id"],
                "spawn_depth": row["spawn_depth"],
                "hook_event_name": row["hook_event_name"],
                "tool_use_id": row["tool_use_id"],
                "tool_name": row["tool_name"],
                "prompt_id": row["prompt_id"],
                "turn_id": row["turn_id"],
                "correlation_id": str(row["correlation_id"]),
                "causation_id": (
                    str(row["causation_id"])
                    if row["causation_id"] is not None
                    else None
                ),
                "emitted_at": _iso(row["emitted_at"]),
                "payload": json.loads(row["payload"]),
                "content_ref_ids": list(row["content_ref_ids"]),
                "source_topic": row["source_topic"],
            }
        return out

    def span_rows_as_contract_json(self) -> dict[tuple[str, str], dict[str, Any]]:
        return {
            key: {
                "session_id": span["session_id"],
                "agent_id": span["agent_id"],
                "agent_type": span["agent_type"],
                "parent_tool_use_id": span["parent_tool_use_id"],
                "parent_agent_id": span["parent_agent_id"],
                "parent_resolution": span["parent_resolution"],
                "workflow_run_id": span["workflow_run_id"],
                "spawn_depth": span["spawn_depth"],
                "started_at": _iso(span["started_at"]),
                "stopped_at": _iso(span["stopped_at"]),
                "tool_call_count": span["tool_call_count"],
            }
            for key, span in self.spans.items()
        }


def _writer(tables: _InMemoryHookTables) -> ClaudeHookEventsProjectionWriter:
    writer = ClaudeHookEventsProjectionWriter()
    writer._db = tables  # type: ignore[assignment]
    return writer


def _deliver(
    writer: ClaudeHookEventsProjectionWriter, event: dict[str, Any]
) -> dict[str, Any]:
    """Hand the writer exactly what the runtime hands it: event plus injections."""
    message: dict[str, Any] = json.loads(json.dumps(event))
    message["_topic"] = SOURCE_TOPIC
    message["_event_type"] = "hook-event"
    message["_partition"] = 0
    message["_offset"] = 7
    message["_db"] = object()
    return writer.handle(message)


# --------------------------------------------------------------------------
# Pins to the capture contract
# --------------------------------------------------------------------------


def test_the_hook_list_matches_the_capture_contract() -> None:
    contract_hooks = set(_capture_contract()["coverage_hooks"])
    assert len(contract_hooks) == 33
    assert {member.value for member in EnumClaudeHookEventName} == contract_hooks


def test_the_payload_allowlist_matches_the_capture_contract() -> None:
    assert set(_capture_contract()["metadata_payload_keys"]) == ALLOWED_PAYLOAD_KEYS


def test_the_subscribe_topic_is_the_capture_contract_topic() -> None:
    assert SOURCE_TOPIC == CAPTURE_TOPIC


def test_every_fixture_payload_key_is_declared() -> None:
    """Positive control for the allowlist: the producer's own events pass it."""
    for path in sorted((FIXTURES / "events").glob("*.json")):
        payload = json.loads(path.read_text("utf-8"))["payload"]
        assert set(payload) <= ALLOWED_PAYLOAD_KEYS, path.name


def test_the_migration_declares_every_contract_column() -> None:
    """Every column the contract's projection declares exists in the DDL."""
    migration = (
        Path(writer_module.__file__).resolve().parent.parent
        / "migrations"
        / "0000_create_claude_hook_events.sql"
    ).read_text(encoding="utf-8")
    projection = _capture_contract()["projection"]
    for table in ("event_table", "lineage_table"):
        for column in projection[table]["columns"]:
            assert f"ADD COLUMN IF NOT EXISTS {column} " in migration, (
                f"{projection[table]['name']}.{column} missing from the DDL"
            )


def test_the_parent_resolution_values_match_the_contract() -> None:
    values = _capture_contract()["projection"]["lineage_table"][
        "parent_resolution_values"
    ]
    assert {member.value for member in EnumParentResolution} == set(values)


# --------------------------------------------------------------------------
# The fold, per hook type
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hook", [member.value for member in EnumClaudeHookEventName], ids=str
)
def test_every_hook_type_fixture_folds_to_one_row(hook: str) -> None:
    event = json.loads((FIXTURES / "events" / f"{hook}.json").read_text("utf-8"))
    result = HandlerProjectionClaudeHookEvents().handle(
        ModelClaudeHookProjectionRequest.model_validate(event)
    )
    row = result.event_row
    assert row.hook_event_name.value == hook
    assert row.session_id == event["lineage"]["session_id"]
    assert row.agent_id == event["lineage"]["agent_id"]
    assert row.payload == event["payload"]
    assert row.content_ref_ids == tuple(
        ref["content_record_id"] for ref in event["content_refs"]
    )
    assert row.source_topic == SOURCE_TOPIC
    assert (result.span_update is None) == (event["lineage"]["agent_id"] is None)


def test_the_fold_is_a_function_of_its_input() -> None:
    event = json.loads((FIXTURES / "events" / "PostToolUse.json").read_text("utf-8"))
    handler = HandlerProjectionClaudeHookEvents()
    first = handler.handle(ModelClaudeHookProjectionRequest.model_validate(event))
    second = handler.handle(ModelClaudeHookProjectionRequest.model_validate(event))
    assert first == second


def test_a_main_thread_parent_is_not_unknown() -> None:
    """The contract's central distinction: found-and-main-thread vs not found."""
    event = _scenario_events("subagent_tree")[2]
    assert event["hook_event_name"] == "SubagentStart"
    wire = ModelClaudeHookEventWire.model_validate(event)
    handler = HandlerProjectionClaudeHookEvents()

    unresolved = handler.handle(ModelClaudeHookProjectionRequest(event=wire))
    assert unresolved.span_update is not None
    assert unresolved.span_update.parent_resolution is EnumParentResolution.UNKNOWN

    resolved = handler.handle(
        ModelClaudeHookProjectionRequest(
            event=wire,
            known_parent_calls=(
                ModelKnownParentToolCall(
                    session_id=wire.lineage.session_id,
                    tool_use_id="toolu_main_1",
                    agent_id=None,
                ),
            ),
        )
    )
    assert resolved.span_update is not None
    assert resolved.span_update.parent_resolution is EnumParentResolution.MAIN_THREAD
    assert resolved.span_update.parent_agent_id is None


# --------------------------------------------------------------------------
# The scenarios, through the real writer entry
# --------------------------------------------------------------------------


def _expected_event_rows() -> dict[str, dict[str, Any]]:
    rows = _jsonl(FIXTURES / "expected_projection" / "claude_hook_events.jsonl")
    out = {}
    for row in rows:
        # ingested_at is projection-side write time: the database assigns it,
        # so the generator's placeholder (equal to emitted_at) is not a value
        # any real writer can reproduce. Every other column is compared.
        row.pop("ingested_at")
        out[row["event_id"]] = row
    return out


def _expected_span_rows() -> dict[tuple[str, str], dict[str, Any]]:
    rows = _jsonl(FIXTURES / "expected_projection" / "claude_agent_spans.jsonl")
    return {(row["session_id"], row["agent_id"]): row for row in rows}


def _run_all_scenarios(tables: _InMemoryHookTables) -> list[dict[str, Any]]:
    writer = _writer(tables)
    return [
        _deliver(writer, event)
        for name in SCENARIOS
        for event in _scenario_events(name)
    ]


def test_scenarios_reproduce_the_contract_expected_rows() -> None:
    tables = _InMemoryHookTables()
    results = _run_all_scenarios(tables)

    assert all(result["event_inserted"] for result in results)
    assert tables.event_rows_as_contract_json() == _expected_event_rows()
    assert tables.span_rows_as_contract_json() == _expected_span_rows()


def test_a_subagents_tool_calls_are_told_apart_from_the_main_threads() -> None:
    """The finding this node answers: same session, three different owners."""
    tables = _InMemoryHookTables()
    _run_all_scenarios(tables)
    tree = [
        row
        for row in tables.event_rows_as_contract_json().values()
        if row["session_id"] == "session-subagent-tree"
        and row["hook_event_name"] == "PostToolUse"
    ]
    owners = {(row["tool_use_id"], row["agent_id"]) for row in tree}
    assert owners == {
        ("toolu_sub_1", "a1"),
        ("toolu_sub_3", "a2"),
        ("toolu_sub_2", "a1"),
        ("toolu_main_1", None),
    }


def test_a_replay_changes_nothing() -> None:
    tables = _InMemoryHookTables()
    _run_all_scenarios(tables)
    events_before = tables.event_rows_as_contract_json()
    spans_before = tables.span_rows_as_contract_json()

    replayed = _run_all_scenarios(tables)

    assert not any(result["event_inserted"] for result in replayed)
    # A redelivered main-thread event writes nothing and says so.
    main_thread = [r for r in replayed if r["agent_id"] is None]
    assert main_thread
    assert all(r["rows_upserted"] == 0 for r in main_thread)
    assert tables.event_rows_as_contract_json() == events_before
    assert tables.span_rows_as_contract_json() == spans_before


def test_a_child_written_before_its_parent_is_repaired() -> None:
    """Out-of-order delivery: unknown until the spawning call arrives."""
    events = _scenario_events("subagent_tree")
    pre_tool_main, subagent_start = events[1], events[2]
    assert pre_tool_main["lineage"]["tool_use_id"] == "toolu_main_1"
    assert subagent_start["lineage"]["parent_tool_use_id"] == "toolu_main_1"

    tables = _InMemoryHookTables()
    writer = _writer(tables)
    _deliver(writer, subagent_start)
    key = ("session-subagent-tree", "a1")
    assert tables.spans[key]["parent_resolution"] == "unknown"

    result = _deliver(writer, pre_tool_main)
    assert result["repaired_child_spans"] == 1
    assert tables.spans[key]["parent_resolution"] == "main_thread"
    assert tables.spans[key]["parent_agent_id"] is None


def test_a_known_parent_is_never_downgraded_to_unknown() -> None:
    events = _scenario_events("subagent_tree")
    tables = _InMemoryHookTables()
    writer = _writer(tables)
    for event in events[:3]:
        _deliver(writer, event)
    key = ("session-subagent-tree", "a1")
    assert tables.spans[key]["parent_resolution"] == "main_thread"

    # A later a1 event whose sidecar was unreadable carries no parent.
    late = json.loads(json.dumps(events[3]))
    late["lineage"]["parent_tool_use_id"] = None
    late["event_id"] = "00000000-0000-5000-8000-000000000001"
    _deliver(writer, late)
    assert tables.spans[key]["parent_resolution"] == "main_thread"
    assert tables.spans[key]["parent_tool_use_id"] == "toolu_main_1"


# --------------------------------------------------------------------------
# Refusals: nothing malformed is stored, and content never is
# --------------------------------------------------------------------------


def _pre_tool_use() -> dict[str, Any]:
    return json.loads((FIXTURES / "events" / "PreToolUse.json").read_text("utf-8"))


def test_a_payload_key_the_contract_does_not_declare_is_refused() -> None:
    event = _pre_tool_use()
    event["payload"]["tool_input"] = {"command": "cat ~/.ssh/id_rsa"}
    tables = _InMemoryHookTables()
    with pytest.raises(ValidationError, match="does not declare"):
        _deliver(_writer(tables), event)
    assert tables.events == {}


def test_a_subagent_flag_that_contradicts_agent_id_is_refused() -> None:
    event = _pre_tool_use()
    event["lineage"]["is_subagent"] = True
    with pytest.raises(ValidationError, match="is_subagent"):
        ModelClaudeHookEventWire.model_validate(event)


def test_content_refs_must_match_the_payload_refs() -> None:
    event = _pre_tool_use()
    event["content_refs"] = []
    with pytest.raises(ValidationError, match="content_refs"):
        ModelClaudeHookEventWire.model_validate(event)


def test_a_mis_unwrapped_event_is_refused_not_stored() -> None:
    """An event published without an envelope loses its lineage on unwrap.

    ``unwrap_envelope`` treats any top-level ``payload`` object as the
    envelope payload, so a bare hook event would arrive as its own payload.
    That must be refused, never stored as a lineage-less row.
    """
    event = _pre_tool_use()
    tables = _InMemoryHookTables()
    with pytest.raises(ValidationError):
        _deliver(_writer(tables), event["payload"])
    assert tables.events == {}


def test_a_timezone_naive_emit_time_is_refused() -> None:
    event = _pre_tool_use()
    event["emitted_at"] = "2026-09-26T12:00:00"
    with pytest.raises(ValidationError):
        ModelClaudeHookEventWire.model_validate(event)


def test_runtime_injected_keys_are_not_part_of_the_event() -> None:
    event = _pre_tool_use()
    event["_envelope"] = {"payload": {}}
    event["_envelope_id"] = "x"
    event["_tenant_id"] = "t"
    assert ModelClaudeHookEventWire.model_validate(event).hook_event_name is (
        EnumClaudeHookEventName.PRE_TOOL_USE
    )
