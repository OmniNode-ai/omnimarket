# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One installed node binding over in-memory and declared Redpanda overlays."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

from omnimarket.models.error_rule_delivery import (
    ModelDeliveryMatch,
    ModelDeliveryOverlay,
)
from omnimarket.nodes.node_error_rule_delivery_effect import node

from .test_delivery_hook import LINE, NOW, Matcher, failure, overlay, request


class FixtureMatcher(Matcher):
    def handle(self, payload):
        if payload.error_text == "unmatched fixture":
            return ModelDeliveryMatch(status="NO-MATCH", class_key="fixture.unmatched")
        return super().handle(payload)


def create_fixture_matcher():
    return FixtureMatcher()


async def run_profile(tmp_path, monkeypatch, bus, profile):
    raw_profile = yaml.safe_load(
        (Path(__file__).parent / "fixtures/overlays" / f"{profile}.yaml").read_text()
    )
    assert raw_profile["transport"] == profile
    deployment = overlay(tmp_path).model_dump(mode="json")
    deployment.update(raw_profile["delivery"])
    deployment["matcher"] = {"module": __name__, "factory": "create_fixture_matcher"}
    config = tmp_path / "delivery.yaml"
    config.write_text(yaml.safe_dump(deployment))
    monkeypatch.setenv("ONEX_ERROR_RULE_DELIVERY_OVERLAY", str(config))
    contract_path = Path(node.__file__).with_name("contract.yaml")
    contract = yaml.safe_load(contract_path.read_text())
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.error-rule-delivery-completed.v1"
    )
    discovery = discover_contracts_from_paths([contract_path])
    assert not discovery.errors
    assert len(discovery.contracts) == 1
    completed = asyncio.Queue()

    async def collect(message):
        await completed.put(json.loads(message.value))

    await bus.subscribe(
        contract["terminal_event"],
        group_id="delivery-results-" + uuid4().hex,
        on_message=collect,
    )
    engine = MessageDispatchEngine()
    report = await wire_from_manifest(
        ModelAutoWiringManifest(contracts=discovery.contracts),
        engine,
        event_bus=bus,
        environment="local",
    )
    assert [result.outcome.value for result in report.results] == ["wired"]
    engine.freeze()

    async def call(command):
        correlation = uuid4()
        envelope = ModelEventEnvelope[object](
            payload=command.model_dump(mode="json"),
            correlation_id=correlation,
            event_type="omnimarket.error-rule-delivery-requested",
        )
        await bus.publish(
            contract["runtime_dispatch"]["command_topic"],
            None,
            envelope.model_dump_json().encode(),
            None,
        )
        result = await asyncio.wait_for(completed.get(), timeout=30)
        assert result["correlation_id"] == str(correlation)
        return result["payload"]

    result = await call(request(failure(tool="Read")))
    assert result["line"] == LINE
    assert result["hook_output"]["hookSpecificOutput"]["additionalContext"] == LINE
    deployment["mode"] = "shadow"
    config.write_text(yaml.safe_dump(deployment))
    result = await call(request(failure(tool="Read", use="shadow")))
    assert result["line"] == ""
    assert result["status"] == "SHADOW"
    deployment["mode"] = "hint"
    config.write_text(yaml.safe_dump(deployment))
    for index in range(2):
        unmatched = failure(use=f"unmatched-{index}").model_copy(
            update={"error_text": "unmatched fixture"}
        )
        assert (await call(request(unmatched)))["status"] == "NO-MATCH"
    assert (await call(request(operation="drain")))["acknowledged"] == 2
    assert (await call(request(operation="drain")))["acknowledged"] == 0
    from omnimarket.models.exception_queue import ModelExceptionQueueRequest
    from omnimarket.nodes.node_exception_queue_effect.handler import (
        HandlerExceptionQueue,
    )

    parsed = ModelDeliveryOverlay.model_validate(deployment)
    queued = HandlerExceptionQueue().handle(
        ModelExceptionQueueRequest(parsed.exception_dir, "orchestrator")
    )
    assert len(queued.rows) == 1


async def test_delivery_hook_inmemory_overlay_routes_declared_command(
    tmp_path, monkeypatch
):
    bus = EventBusInmemory(environment="local", group="delivery-" + uuid4().hex)
    await bus.start()
    try:
        await run_profile(tmp_path, monkeypatch, bus, "inmemory")
    finally:
        await bus.close()


@pytest.mark.kafka
@pytest.mark.integration
async def test_delivery_hook_redpanda_overlay_routes_declared_command(
    tmp_path, monkeypatch
):
    # Exercise the production lane adapter, including declared transport and
    # credential-store identity. A plaintext raw client is not this path.
    from contextlib import nullcontext

    from omnibase_infra.cli.delegate_lane_credentials import resolve_lane_binding
    from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
    from omnibase_infra.event_bus.lane_client_transport_binding import (
        bind_lane_client_transport,
    )

    from tests.bus_lane_guard import assert_broker_in_declared_lane, declared_test_lane

    profile = yaml.safe_load(
        (Path(__file__).parent / "fixtures/overlays/redpanda.yaml").read_text()
    )
    declaration, lane_overlay = (
        Path(profile["lane_declaration"]),
        Path(profile["lane_overlay"]),
    )
    selection, transport = resolve_lane_binding(declared_test_lane(declaration))
    assert_broker_in_declared_lane(
        selection.bootstrap_servers,
        declaration_path=declaration,
        overlay_path=lane_overlay,
    )
    binding = (
        bind_lane_client_transport(transport)
        if transport is not None
        else nullcontext()
    )
    with binding:
        bus = EventBusKafka.from_bootstrap(selection.bootstrap_servers)
        await bus.start()
        try:
            await run_profile(tmp_path, monkeypatch, bus, "redpanda")
        finally:
            await bus.close()


async def test_delivery_hook_health_signal_routes_through_existing_alarm_node(
    tmp_path, monkeypatch
):
    from omnimarket.models.error_rule_delivery import (
        ModelDeliveryAlarmRequest,
        ModelDeliverySignal,
    )
    from omnimarket.nodes.node_silent_failure_alarm_effect import node as alarm_node
    from omnimarket.nodes.node_silent_failure_alarm_effect.models import (
        ModelSilentFailureAlarmOptions,
    )

    config = tmp_path / "alarm.json"
    config.write_text(
        ModelSilentFailureAlarmOptions(registry_root=tmp_path).model_dump_json()
    )
    monkeypatch.setenv("ONEX_SILENT_FAILURE_ALARM_CONFIG", str(config))
    path = Path(alarm_node.__file__).with_name("contract.yaml")
    raw = yaml.safe_load(path.read_text())
    discovered = discover_contracts_from_paths([path])
    assert not discovered.errors
    completed = asyncio.Queue()
    bus = EventBusInmemory(environment="local", group="health-" + uuid4().hex)

    async def collect(message):
        await completed.put(json.loads(message.value))

    await bus.start()
    try:
        await bus.subscribe(
            raw["terminal_event"],
            group_id="health-result-" + uuid4().hex,
            on_message=collect,
        )
        engine = MessageDispatchEngine()
        report = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=discovered.contracts),
            engine,
            event_bus=bus,
            environment="local",
        )
        assert [r.outcome.value for r in report.results] == ["wired"]
        engine.freeze()
        correlation = uuid4()
        command = ModelDeliveryAlarmRequest(
            now=NOW,
            apply=False,
            signals=(
                ModelDeliverySignal(
                    surface="tool_failure",
                    cause="probe-not-seen",
                    detail="installed hook entry removed",
                ),
            ),
        )
        envelope = ModelEventEnvelope[object](
            payload=command.model_dump(mode="json"),
            correlation_id=correlation,
            event_type="omnimarket.delivery-health-alarm-requested",
        )
        topic = next(
            row["topic"]
            for row in raw["handler_routing"]["handlers"]
            if row["operation"] == "delivery-health"
        )
        await bus.publish(topic, None, envelope.model_dump_json().encode(), None)
        result = await asyncio.wait_for(completed.get(), timeout=3)
        assert result["correlation_id"] == str(correlation)
        assert result["payload"]["raised"]
        assert "cause=probe-not-seen" in result["payload"]["rows"][0]
    finally:
        await bus.close()
