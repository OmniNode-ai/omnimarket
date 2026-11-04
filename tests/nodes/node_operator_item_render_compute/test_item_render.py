# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tick must surface the whole tracked fold, rather than only operator questions."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.models.model_ask_state import (
    EnumAskState,
    ModelAskEntry,
    ModelAskStateReport,
)
from omnimarket.nodes.node_operator_waiting_tick_effect.models import (
    ModelOperatorWaitingRequest,
)
from omnimarket.nodes.node_operator_waiting_tick_effect.node import (
    NodeOperatorWaitingTickEffect,
)

NOW = datetime(2026, 11, 4, 18, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[3]


def report(count: int = 212) -> ModelAskStateReport:
    return ModelAskStateReport(
        schema_id="omni.ask-state/1",
        generated_at=NOW,
        since=NOW - timedelta(days=30),
        asks=tuple(
            ModelAskEntry(
                ask_id=f"ask-{i}",
                item_id=f"item-{i}",
                kind=("ask", "ruling", "promise", "question")[i % 4],
                said_at=NOW - timedelta(minutes=i + 1),
                session_id="session",
                words=f"Words for item {i}",
                subject="",
                confidence=1.0,
                state="owned",
                owner_lane=f"owner-{i % 3}",
                running_lane=None if i % 5 == 0 else f"owner-{i % 3}",
                orphaned=i % 5 == 0,
                age_minutes=i + 1,
            )
            for i in range(count)
        ),
    )


def test_item_render_waiting_lists_all_212_tracked_items(tmp_path: Path) -> None:
    """The previous caller passed only MSG question= rows and silently lost the other kinds."""
    from omnimarket.models.model_operator_item_render import (
        ModelOperatorItemRenderRequest,
    )

    cfg = yaml.safe_load(
        (
            ROOT
            / "src/omnimarket/nodes/node_operator_item_render_compute/contract.yaml"
        ).read_text()
    )["config"]
    result = NodeOperatorWaitingTickEffect().handle(
        ModelOperatorWaitingRequest(
            items=ModelOperatorItemRenderRequest(report=report(), now=NOW, config=cfg),
            ledger=str(tmp_path / "ledger.md"),
            resolver=tmp_path / "absent-writer.py",
            lane="test",
            timeout=1,
        )
    )
    assert (
        result.lines[0]
        == "OPEN-ITEMS n=212 unowned=43 oldest=3h32m as_of=2026-11-04T18:00:00Z"
    )
    assert len([line for line in result.lines if line.startswith("  ")]) == 212
    for i in range(212):
        assert sum(f"id=ask-{i}:" in line for line in result.lines) == 1


def test_item_render_throughput_missing_snapshot_is_unknown() -> None:
    import json

    from omnimarket.nodes.node_throughput_tick_decision_compute import (
        NodeThroughputTickDecisionCompute,
    )
    from omnimarket.nodes.node_throughput_tick_decision_compute.models.model_throughput_tick_decision import (
        ModelThroughputTickRequest,
    )

    cases = json.loads(
        (ROOT / "tests/fixtures/throughput_tick_parity.json").read_text()
    )["cases"]
    healthy = next(
        case
        for case in cases
        if case["expected"]["status_line"].startswith("THROUGHPUT OK")
    )
    result = NodeThroughputTickDecisionCompute().handle(
        ModelThroughputTickRequest.model_validate(healthy["request"])
    )
    assert any(line.startswith("UNKNOWN open-items") for line in result.lines)
    assert "open-items" in result.unknown
    assert result.exit_code == 1
    assert " OK " not in result.status_line


def render_request(count: int = 212, **updates):
    from omnimarket.models.model_operator_item_render import (
        ModelOperatorItemRenderRequest,
    )

    config = yaml.safe_load(
        (
            ROOT
            / "src/omnimarket/nodes/node_operator_item_render_compute/contract.yaml"
        ).read_text()
    )["config"]
    return ModelOperatorItemRenderRequest.model_validate(
        {"report": report(count), "now": NOW, "config": config, **updates}
    )


def throughput_request(items=None):
    import json

    from omnimarket.nodes.node_throughput_tick_decision_compute.models.model_throughput_tick_decision import (
        ModelThroughputTickRequest,
    )

    cases = json.loads(
        (ROOT / "tests/fixtures/throughput_tick_parity.json").read_text()
    )["cases"]
    healthy = next(
        case
        for case in cases
        if case["expected"]["status_line"].startswith("THROUGHPUT OK")
    )
    # Preserve every interval of the recorded healthy fixture while moving it to this replay's time.
    old_now = datetime.fromisoformat(healthy["request"]["now"].replace("Z", "+00:00"))
    offset = NOW - old_now

    def move(value):
        if isinstance(value, dict):
            return {key: move(item) for key, item in value.items()}
        if isinstance(value, list):
            return [move(item) for item in value]
        if isinstance(value, str):
            try:
                dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError:
                return value
            if dt.tzinfo:
                return (dt + offset).strftime("%Y-%m-%dT%H:%M:%SZ")
        return value

    return ModelThroughputTickRequest.model_validate(
        {**move(healthy["request"]), "operator_items": items}
    )


def test_item_render_same_block_from_both_ticks_over_one_fixture_ledger(
    tmp_path: Path,
) -> None:
    import json

    from omnimarket.models.model_ask_state import ModelAskStateRequest
    from omnimarket.models.model_operator_item_render import (
        ModelOperatorItemsReadRequest,
    )
    from omnimarket.nodes.node_ask_state_compute.node import NodeAskStateCompute
    from omnimarket.nodes.node_operator_item_render_compute import (
        NodeOperatorItemRenderCompute,
    )
    from omnimarket.nodes.node_throughput_tick_decision_compute import (
        NodeThroughputTickDecisionCompute,
    )

    fixture = (
        ROOT / "tests/unit/nodes/node_ask_state_compute/fixtures/tracked_items.json"
    )
    raw = json.loads(fixture.read_text())
    raw["config"] = yaml.safe_load(NodeAskStateCompute.contract_path.read_text())[
        "config"
    ]
    ledger = tmp_path / "ledger.md"
    ledger.write_text("\n".join(raw.pop("rows")))
    fold = ModelAskStateRequest.model_validate({**raw, "rows": (), "now": NOW})
    waiting_node = NodeOperatorWaitingTickEffect()
    items = waiting_node.read_items(
        ModelOperatorItemsReadRequest(
            ledger_path=ledger,
            fold=fold,
            config=render_request().config,
        )
    )
    expected = NodeOperatorItemRenderCompute().handle(items)
    assert expected.known
    assert expected.count
    assert expected.overdue
    writer = tmp_path / "writer.py"
    writer.write_text("print('recorded')\n")
    waiting = waiting_node.handle(
        ModelOperatorWaitingRequest(
            items=items,
            ledger=str(ledger),
            resolver=writer,
            lane="test",
            timeout=5,
        )
    )
    throughput = NodeThroughputTickDecisionCompute().handle(throughput_request(items))
    assert waiting.exit_code == 0
    assert tuple(waiting.lines) == expected.lines
    assert tuple(throughput.lines[-len(expected.lines) :]) == expected.lines
    assert throughput.status_line.startswith("THROUGHPUT OK")


def test_item_render_removed_ledger_is_unknown_and_never_ok(tmp_path: Path) -> None:
    import json

    from omnimarket.models.model_ask_state import ModelAskStateRequest
    from omnimarket.models.model_operator_item_render import (
        ModelOperatorItemsReadRequest,
    )
    from omnimarket.nodes.node_ask_state_compute.node import NodeAskStateCompute
    from omnimarket.nodes.node_throughput_tick_decision_compute import (
        NodeThroughputTickDecisionCompute,
    )

    raw = json.loads(
        (
            ROOT / "tests/unit/nodes/node_ask_state_compute/fixtures/tracked_items.json"
        ).read_text()
    )
    raw["config"] = yaml.safe_load(NodeAskStateCompute.contract_path.read_text())[
        "config"
    ]
    ledger = tmp_path / "removed-ledger.md"
    ledger.write_text("\n".join(raw["rows"]))
    ledger.unlink()
    waiting_node = NodeOperatorWaitingTickEffect()
    items = waiting_node.read_items(
        ModelOperatorItemsReadRequest(
            ledger_path=ledger,
            fold=ModelAskStateRequest.model_validate(raw),
            config=render_request().config,
        )
    )
    assert items.report is None
    assert "FileNotFoundError" in items.source_error
    waiting = waiting_node.handle(
        ModelOperatorWaitingRequest(
            items=items,
            ledger=str(ledger),
            resolver=tmp_path / "not-called",
            lane="test",
            timeout=1,
        )
    )
    throughput = NodeThroughputTickDecisionCompute().handle(throughput_request(items))
    assert waiting.lines[0].startswith("UNKNOWN open-items")
    assert waiting.exit_code == throughput.exit_code == 1
    assert "open-items" in throughput.unknown
    assert " OK " not in throughput.status_line
    assert not any(line.startswith("OPEN-ITEMS n=0") for line in throughput.lines)


def test_item_render_old_item_is_visible_on_otherwise_ok_tick() -> None:
    from omnimarket.nodes.node_operator_item_render_compute import (
        NodeOperatorItemRenderCompute,
    )
    from omnimarket.nodes.node_throughput_tick_decision_compute import (
        NodeThroughputTickDecisionCompute,
    )

    items = render_request(1)
    old = items.report.asks[0].model_copy(
        update={"said_at": NOW - timedelta(days=2), "age_minutes": 1}
    )
    items = items.model_copy(
        update={"report": items.report.model_copy(update={"asks": (old,)})}
    )
    expected = NodeOperatorItemRenderCompute().handle(items)
    result = NodeThroughputTickDecisionCompute().handle(throughput_request(items))
    assert expected.overdue == (old.ask_id,)
    assert "oldest=2d00h" in expected.lines[0]
    assert result.status_line.startswith("THROUGHPUT OK")
    assert tuple(result.lines[-len(expected.lines) :]) == expected.lines
    assert any("id=ask-0:" in line for line in result.lines)


def test_item_render_groups_by_owner_oldest_first_clips_words_and_marks_new() -> None:
    from omnimarket.nodes.node_operator_item_render_compute import (
        NodeOperatorItemRenderCompute,
    )

    source = render_request(6, new={"ask-5", "closed", "not-present"})
    original = source.report.asks
    entries = (
        original[0].model_copy(
            update={
                "owner_lane": "owner-2",
                "state": EnumAskState.NEW,
                "words": "\n" + "é" * 200 + "\nmore words",
            }
        ),
        *original[1:],
        original[0].model_copy(
            update={
                "ask_id": "closed",
                "state": EnumAskState.DONE,
                "said_at": NOW - timedelta(days=7),
            }
        ),
    )
    source = source.model_copy(
        update={
            "report": source.report.model_copy(
                update={"asks": tuple(reversed(entries))}
            )
        }
    )
    result = NodeOperatorItemRenderCompute().handle(source)
    item_lines = [line for line in result.lines if line.startswith("  ")]
    assert len(item_lines) == 6
    assert result.count == 6
    assert result.unowned == 2
    assert [line for line in result.lines if line.startswith("owner=")] == [
        "owner=owner-2 (3)",
        "owner=owner-1 (2)",
        "owner=owner-0 (1)",
    ]
    assert [line.split("id=")[1].split(":")[0] for line in item_lines] == [
        "ask-5",
        "ask-2",
        "ask-0",
        "ask-4",
        "ask-1",
        "ask-3",
    ]
    assert " NEW " in item_lines[0]
    assert "state=new NEW" in item_lines[2]
    assert item_lines[2].split(": ")[1] == "é" * 160
    assert "closed" not in "\n".join(result.lines)
    assert NodeOperatorItemRenderCompute().handle(source) == result


def test_item_render_empty_and_failed_reads_are_distinct() -> None:
    from omnimarket.nodes.node_operator_item_render_compute import (
        NodeOperatorItemRenderCompute,
    )

    empty = NodeOperatorItemRenderCompute().handle(render_request(0))
    assert empty.known
    assert empty.count == empty.unowned == 0
    assert empty.lines == (
        "OPEN-ITEMS n=0 unowned=0 oldest=none as_of=2026-11-04T18:00:00Z",
    )
    failed = NodeOperatorItemRenderCompute().handle(
        render_request(0, source_error="permission\n denied")
    )
    assert not failed.known
    assert failed.count is None
    assert failed.unowned is None
    assert failed.lines == ("UNKNOWN open-items | permission denied",)


def test_item_render_overlay_controls_age_limit_and_excerpt() -> None:
    from omnimarket.nodes.node_operator_item_render_compute import (
        NodeOperatorItemRenderCompute,
    )

    source = render_request(2)
    source = source.model_copy(
        update={
            "config": source.config.model_copy(
                update={"age_limit_minutes": 1, "words_chars": 5}
            )
        }
    )
    result = NodeOperatorItemRenderCompute().handle(source)
    assert result.overdue == ("ask-1",)
    assert all(
        line.endswith(": Words") for line in result.lines if line.startswith("  ")
    )


def test_item_render_both_ticks_call_the_shared_node_sentinel(
    tmp_path: Path, monkeypatch
) -> None:
    from omnimarket.models.model_operator_item_render import (
        ModelOperatorItemRenderResult,
    )
    from omnimarket.nodes.node_operator_item_render_compute import (
        NodeOperatorItemRenderCompute,
    )
    from omnimarket.nodes.node_throughput_tick_decision_compute import (
        NodeThroughputTickDecisionCompute,
    )

    calls = []

    def sentinel(self, request):
        calls.append(request)
        return ModelOperatorItemRenderResult(
            lines=("SENTINEL omnimarket renderer",), known=True, count=0, unowned=0
        )

    monkeypatch.setattr(NodeOperatorItemRenderCompute, "handle", sentinel)
    source = render_request(0)
    waiting = NodeOperatorWaitingTickEffect().handle(
        ModelOperatorWaitingRequest(
            items=source,
            ledger="unused",
            resolver=tmp_path / "unused",
            lane="test",
            timeout=1,
        )
    )
    throughput = NodeThroughputTickDecisionCompute().handle(throughput_request(source))
    assert waiting.lines == ["SENTINEL omnimarket renderer"]
    assert throughput.lines[-1] == waiting.lines[0]
    assert calls == [source, source]


async def exchange(
    bus, source, package="node_operator_item_render_compute", operation=None
):
    import asyncio
    import importlib
    import json
    from uuid import uuid4

    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
    from omnibase_infra.event_bus.topic_constants import (
        derive_event_type_alias_for_topic,
    )
    from omnibase_infra.runtime.auto_wiring.discovery import (
        discover_contracts_from_paths,
    )
    from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
    from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
    from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

    node_module = importlib.import_module("omnimarket.nodes." + package)
    path = Path(node_module.__file__).with_name("contract.yaml")
    contract = yaml.safe_load(path.read_text())
    topic = contract["runtime_dispatch"]["command_topic"]
    if operation:
        topic = next(
            row["topic"]
            for row in contract["handler_routing"]["handlers"]
            if row["operation"] == operation
        )
    correlation = uuid4()
    received = asyncio.Queue()

    async def collect(message):
        envelope = json.loads(message.value)
        if envelope["correlation_id"] == str(correlation):
            await received.put(envelope["payload"])

    terminal = contract["terminal_event"]
    if operation:
        result_model = next(
            row["output_model"]
            for row in contract["handler_routing"]["handlers"]
            if row["operation"] == operation
        ).rsplit(".", 1)[-1]
        terminal = next(
            row["topic"]
            for row in contract["published_events"]
            if row["event_type"] == result_model
        )
        assert terminal != contract["terminal_event"]
    await bus.subscribe(
        terminal,
        group_id="item-render-" + correlation.hex,
        on_message=collect,
    )
    discovery = discover_contracts_from_paths([path])
    assert not discovery.errors
    engine = MessageDispatchEngine()
    wiring = await wire_from_manifest(
        ModelAutoWiringManifest(contracts=tuple(discovery.contracts)),
        engine,
        event_bus=bus,
        environment="local",
    )
    assert [row.outcome.value for row in wiring.results] == ["wired"]
    engine.freeze()
    envelope = ModelEventEnvelope[object](
        payload=source.model_dump(mode="json"),
        correlation_id=correlation,
        event_type=derive_event_type_alias_for_topic(topic),
    )
    await bus.publish(topic, None, envelope.model_dump_json().encode(), None)
    return await asyncio.wait_for(received.get(), timeout=30)


async def test_item_render_installed_discovery_and_inmemory_overlay() -> None:
    from importlib.metadata import entry_points

    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

    from omnimarket.handlers.bus_binding.node_event_bus_overlay import (
        read_node_event_bus,
    )
    from omnimarket.nodes.node_operator_item_render_compute import (
        NodeOperatorItemRenderCompute,
    )

    registered = entry_points(group="onex.nodes").select(
        name="node_operator_item_render_compute"
    )
    assert len(registered) == 1
    assert (
        next(iter(registered)).load().NodeOperatorItemRenderCompute
        is NodeOperatorItemRenderCompute
    )
    overlay = Path(__file__).with_name("fixtures") / "overlays/inmemory.yaml"
    assert read_node_event_bus(overlay).type == "inmemory"
    source = render_request(config=yaml.safe_load(overlay.read_text())["config"])
    bus = EventBusInmemory(environment="local", group="item-render")
    await bus.start()
    try:
        result = await exchange(bus, source)
    finally:
        await bus.close()
    assert result == NodeOperatorItemRenderCompute().handle(source).model_dump(
        mode="json"
    )
    assert result["count"] == 212


async def test_item_render_read_operation_is_wired_to_the_live_ledger(
    tmp_path: Path,
) -> None:
    import json

    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

    from omnimarket.models.model_ask_state import ModelAskStateRequest
    from omnimarket.models.model_operator_item_render import (
        ModelOperatorItemsReadRequest,
    )
    from omnimarket.nodes.node_ask_state_compute.node import NodeAskStateCompute

    raw = json.loads(
        (
            ROOT / "tests/unit/nodes/node_ask_state_compute/fixtures/tracked_items.json"
        ).read_text()
    )
    raw["config"] = yaml.safe_load(NodeAskStateCompute.contract_path.read_text())[
        "config"
    ]
    ledger = tmp_path / "ledger.md"
    ledger.write_text("\n".join(raw["rows"]))
    source = ModelOperatorItemsReadRequest(
        ledger_path=ledger,
        fold=ModelAskStateRequest.model_validate(raw),
        config=render_request().config,
    )
    expected = (
        NodeOperatorWaitingTickEffect().read_items(source).model_dump(mode="json")
    )
    bus = EventBusInmemory(environment="local", group="item-read")
    await bus.start()
    try:
        result = await exchange(
            bus, source, "node_operator_waiting_tick_effect", "read_items"
        )
    finally:
        await bus.close()
    assert result == expected


@pytest.mark.kafka
@pytest.mark.integration
async def test_item_render_redpanda_overlay_matches_inmemory() -> None:
    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

    from omnimarket.handlers.bus_binding.node_event_bus_overlay import (
        read_node_event_bus,
    )

    overlay = Path(__file__).with_name("fixtures") / "overlays/redpanda.yaml"
    declaration = read_node_event_bus(overlay)
    assert declaration.type == "kafka"
    assert declaration.lane == "dev"
    source = render_request(config=yaml.safe_load(overlay.read_text())["config"])
    memory = EventBusInmemory(environment="local", group="item-render-parity")
    await memory.start()
    try:
        expected = await exchange(memory, source)
    finally:
        await memory.close()
    from contextlib import nullcontext

    from omnibase_infra.cli.delegate_lane_credentials import resolve_lane_binding
    from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
    from omnibase_infra.event_bus.lane_client_transport_binding import (
        bind_lane_client_transport,
    )

    from tests.bus_lane_guard import declared_test_lane

    assert declaration.lane == declared_test_lane()
    target, transport = resolve_lane_binding(declaration.lane)
    binding = (
        bind_lane_client_transport(transport)
        if transport is not None
        else nullcontext()
    )
    with binding:
        broker = EventBusKafka.from_bootstrap(target.bootstrap_servers)
        await broker.start()
        try:
            assert await exchange(broker, source) == expected
        finally:
            await broker.close()
