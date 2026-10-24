# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Metadata-only Beacon capture, pinned to the hook projection's wire model."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_beacon_trace_capture_effect.__main__ import main
from omnimarket.nodes.node_beacon_trace_capture_effect.handlers.adapter_io import (
    FileBeaconLogReader,
    KafkaHookEventPublisher,
    publish_topics,
)
from omnimarket.nodes.node_beacon_trace_capture_effect.handlers.handler_beacon_event_mapping import (
    NAMESPACE,
    map_beacon_line,
)
from omnimarket.nodes.node_beacon_trace_capture_effect.handlers.handler_beacon_trace_capture import (
    HandlerBeaconTraceCapture,
)
from omnimarket.nodes.node_beacon_trace_capture_effect.models.model_beacon_hook_event import (
    ModelBeaconHookEvent,
)
from omnimarket.nodes.node_beacon_trace_capture_effect.models.model_beacon_trace_capture import (
    ModelBeaconTraceCaptureCommand,
)
from omnimarket.nodes.node_projection_claude_hook_events.models.model_claude_hook_event_wire import (
    ALLOWED_PAYLOAD_KEYS,
    ModelClaudeHookEventWire,
)

FIXTURE = Path(__file__).parent / "fixtures/beacon_codex_runtime_placeholder.jsonl"
NODE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_beacon_trace_capture_effect"
)
NAMES = [
    "SessionStart",
    "UserPromptSubmit",
    "PermissionRequest",
    "PostToolUse",
    "PostToolUse",
    "AssistantUsage",
]
LANE = "beacon-test-lane"
HOST = "lab-test-host"


class MemoryReader:
    def __init__(self, lines: list[str] | None = None) -> None:
        self.lines = lines if lines is not None else FIXTURE.read_text().splitlines()
        self.calls: list[tuple[str, int]] = []

    def read_from(self, path: str, offset: int) -> tuple[list[str], int]:
        self.calls.append((path, offset))
        return self.lines, FIXTURE.stat().st_size


class MemoryPublisher:
    def __init__(self) -> None:
        self.published: list[tuple[str, str, ModelBeaconHookEvent]] = []

    def publish(self, topic: str, key: str, event: ModelBeaconHookEvent) -> None:
        self.published.append((topic, key, event))


def command(**overrides: Any) -> ModelBeaconTraceCaptureCommand:
    values = {"log_path": str(FIXTURE), "offset": 0, "lane": LANE, "host": HOST}
    return ModelBeaconTraceCaptureCommand.model_validate(values | overrides)


def capture() -> tuple[list[ModelBeaconHookEvent], MemoryPublisher]:
    publisher = MemoryPublisher()
    HandlerBeaconTraceCapture(reader=MemoryReader(), publisher=publisher).handle(
        command()
    )
    return [event for _, _, event in publisher.published], publisher


def test_beacon_fixture_maps_to_hook_events() -> None:
    publisher = MemoryPublisher()
    result = HandlerBeaconTraceCapture(
        reader=MemoryReader(), publisher=publisher
    ).handle(command())
    events = [event for _, _, event in publisher.published]
    assert result.lines_read == 7
    assert result.skipped == 1
    assert result.malformed == 0
    assert result.events_published == 6
    assert [event.hook_event_name for event in events] == NAMES
    assert all(event.actor == "codex" for event in events)
    assert all(event.lane == LANE and event.host == HOST for event in events)
    assert result.event_ids == tuple(event.event_id for event in events)
    assert result.hook_event_counts == dict(Counter(NAMES))


def test_beacon_events_validate_against_hook_event_wire() -> None:
    events, _ = capture()
    for event in events:
        wire = ModelClaudeHookEventWire.model_validate(event.model_dump(mode="json"))
        assert wire.event_id == event.event_id
        assert set(event.payload) <= ALLOWED_PAYLOAD_KEYS
        assert event.content_refs == ()
        assert event.emitted_at.utcoffset() is not None


def test_beacon_events_carry_no_content() -> None:
    events, _ = capture()
    forbidden = {
        "prompt",
        "text",
        "arguments",
        "output",
        "command",
        "user.email",
        "user.account_id",
    }
    for event in events:
        assert "<placeholder>" not in event.model_dump_json()
        assert not (set(event.payload) & forbidden)
        assert all(value is not None for value in event.payload.values())


def test_beacon_mapping_is_deterministic() -> None:
    lines = [json.loads(line) for line in FIXTURE.read_text().splitlines()[1:]]
    events = [map_beacon_line(line, lane=LANE, host=HOST) for line in lines]
    for line, event in zip(lines, events, strict=True):
        assert event is not None
        again = map_beacon_line(line, lane=LANE, host=HOST)
        assert again == event
        assert event.event_id == uuid5(NAMESPACE, f"beacon:{line['event']['id']}")
        assert event.lineage.correlation_id == uuid5(
            NAMESPACE, f"beacon-correlation:{line['session']['id']}"
        )
    assert len({event.event_id for event in events if event is not None}) == 6
    assert len({event.lineage.correlation_id for event in events if event}) == 1


def test_beacon_failure_and_malformed() -> None:
    line = json.loads(FIXTURE.read_text().splitlines()[5])
    line["raw"]["attributes"]["success"] = False
    publisher = MemoryPublisher()
    result = HandlerBeaconTraceCapture(
        reader=MemoryReader(["not JSON", "[]", json.dumps(line)]), publisher=publisher
    ).handle(command())
    assert result.malformed == 2
    assert result.skipped == 0
    assert result.lines_read == 3
    assert result.events_published == 1
    event = publisher.published[0][2]
    assert event.hook_event_name == "PostToolUseFailure"
    ModelClaudeHookEventWire.model_validate(event.model_dump(mode="json"))


def test_beacon_handler_publishes_on_contract_topic() -> None:
    topic = yaml.safe_load((NODE / "contract.yaml").read_text())["event_bus"][
        "publish_topics"
    ][0]
    reader, publisher = MemoryReader(), MemoryPublisher()
    handler = HandlerBeaconTraceCapture(reader=reader, publisher=publisher)
    result = handler.handle(command(offset=12))
    assert reader.calls == [(str(FIXTURE), 12)]
    assert result.next_offset == FIXTURE.stat().st_size
    assert len(publisher.published) == 6
    for actual_topic, key, event in publisher.published:
        assert actual_topic == topic
        assert key == event.lineage.session_id
    dry_publisher = MemoryPublisher()
    dry = HandlerBeaconTraceCapture(
        reader=MemoryReader(), publisher=dry_publisher
    ).handle(command(dry_run=True))
    assert dry_publisher.published == []
    assert dry.events_published == 0
    assert dry.model_dump(exclude={"events_published"}) == result.model_dump(
        exclude={"events_published"}
    )


def test_beacon_reader_leaves_partial_line(tmp_path: Path) -> None:
    path = tmp_path / "beacon.jsonl"
    complete = '{"unicode": "é"}\n'
    path.write_bytes((complete + '{"partial":').encode())
    reader = FileBeaconLogReader()
    lines, offset = reader.read_from(str(path), 0)
    assert lines == [complete.rstrip("\n")]
    assert offset == len(complete.encode())
    assert reader.read_from(str(path), offset) == ([], offset)
    with path.open("ab") as stream:
        stream.write(b"true}\n")
    assert reader.read_from(str(path), offset) == (
        ['{"partial":true}'],
        path.stat().st_size,
    )


@pytest.mark.parametrize(
    "action", ["prompt.submitted", "command.executed", "tool.invoked"]
)
def test_beacon_mapping_preserves_only_typed_metadata(action: str) -> None:
    line = json.loads(FIXTURE.read_text().splitlines()[4])
    line["event"]["action"] = action
    line["raw"]["turn_id"] = "turn-123"
    line["raw"]["attributes"].update(
        call_id="call-123", duration_ms=42, prompt_length=17, success=True
    )
    event = map_beacon_line(line, lane=None, host=None)
    assert event is not None
    assert event.host is None
    assert event.lane is None
    assert event.lineage.tool_use_id == "call-123"
    assert event.lineage.turn_id == "turn-123"
    key = "prompt_length" if action == "prompt.submitted" else "duration_ms"
    assert event.payload[key] == (17 if key == "prompt_length" else 42)
    assert event.payload["source"] == "beacon"
    ModelClaudeHookEventWire.model_validate(event.model_dump(mode="json"))


def test_beacon_usage_counts_and_missing_model() -> None:
    line = json.loads(FIXTURE.read_text().splitlines()[6])
    event = map_beacon_line(line, lane=LANE, host=HOST)
    assert event is not None
    assert event.payload == {
        "hook_event_name": "AssistantUsage",
        "model": "<model>",
        "message_count": 1,
        "input_tokens": 30118,
        "output_tokens": 62,
        "cache_read_input_tokens": 27136,
        "cache_creation_input_tokens": 0,
        "usage_source_hook": "beacon",
        "source": "beacon",
    }
    del line["raw"]["attributes"]["codex.turn.token_usage.cached_input_tokens"]
    del line["raw"]["attributes"]["codex.turn.token_usage.cache_write_input_tokens"]
    defaulted = map_beacon_line(line, lane=LANE, host=HOST)
    assert defaulted is not None
    assert defaulted.payload["cache_read_input_tokens"] == 0
    assert defaulted.payload["cache_creation_input_tokens"] == 0
    for model in (None, "", "   "):
        line["model"] = model
        assert map_beacon_line(line, lane=LANE, host=HOST) is None


def test_beacon_mapping_skips_unrelated_lines() -> None:
    line = json.loads(FIXTURE.read_text().splitlines()[1])
    line["event"]["action"] = "unknown.action"
    assert map_beacon_line(line, lane=LANE, host=HOST) is None
    line["event"]["action"] = "session.started"
    del line["session"]
    assert map_beacon_line(line, lane=LANE, host=HOST) is None
    assert map_beacon_line({}, lane=LANE, host=HOST) is None


def test_beacon_models_are_frozen_and_forbid_extras() -> None:
    event = capture()[0][0]
    with pytest.raises(ValidationError, match="frozen"):
        event.actor = "codex"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ModelBeaconHookEvent.model_validate(event.model_dump() | {"harness": "codex"})
    with pytest.raises(ValidationError, match="extra_forbidden"):
        type(event.lineage).model_validate(
            event.lineage.model_dump() | {"text": "secret"}
        )
    with pytest.raises(ValidationError, match="greater_than_equal"):
        command(offset=-1)


def test_beacon_publisher_refuses_undeclared_topic() -> None:
    publisher = KafkaHookEventPublisher()
    event = capture()[0][0]
    with pytest.raises(ValueError, match="undeclared"):
        publisher.publish("undeclared-topic", event.lineage.session_id, event)


def test_beacon_cli_dry_run(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--log-path", str(FIXTURE), "--host", HOST, "--dry-run"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["lines_read"] == 7
    assert output["events_published"] == 0
    assert output["hook_event_counts"] == dict(Counter(NAMES))
    assert all(isinstance(UUID(value), UUID) for value in output["event_ids"])


def test_beacon_cli_refuses_negative_offset() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--log-path", str(FIXTURE), "--offset", "-1"])
    assert exc.value.code == 2


def test_beacon_publisher_bus_lane_uses_declared_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    import omnibase_infra.cli.delegate_lane_credentials as lane_credentials

    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "env-broker.invalid:9092")
    monkeypatch.setattr(
        lane_credentials,
        "resolve_lane_binding",
        lambda lane: (SimpleNamespace(bootstrap_servers=f"{lane}.invalid:19092"), None),
    )
    flushed: list[str] = []

    async def fake_publish(self: KafkaHookEventPublisher, brokers: str) -> None:
        flushed.append(brokers)

    monkeypatch.setattr(KafkaHookEventPublisher, "_publish_kafka", fake_publish)
    publisher = KafkaHookEventPublisher(bus_lane="dev")
    event = capture()[0][0]
    topic = yaml.safe_load((NODE / "contract.yaml").read_text())["event_bus"][
        "publish_topics"
    ][0]
    publisher.publish(topic, event.lineage.session_id, event)
    publisher.flush()
    assert flushed == ["dev.invalid:19092"]


def test_beacon_publishes_on_the_hook_capture_topic() -> None:
    projection = yaml.safe_load(
        (
            NODE.parent / "node_projection_claude_hook_events" / "contract.yaml"
        ).read_text()
    )
    assert publish_topics() == ("onex.evt.omniclaude.hook-event.v1",)
    assert publish_topics()[0] in projection["event_bus"]["subscribe_topics"]
