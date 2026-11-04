# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Stop decisions over the repo's declared Redpanda test-lane overlay."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

from omnimarket.models.model_operator_stop_gate import ModelUnansweredGuardRequest

pytestmark = [pytest.mark.integration, pytest.mark.kafka]
CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_operator_access_mode_effect/contract.yaml"
)


@pytest.fixture(autouse=True)
def _isolated_topic_namespace(monkeypatch):
    monkeypatch.setenv("KAFKA_TOPIC_NAMESPACE", "unanswered-guard-" + uuid4().hex[:12])


@pytest_asyncio.fixture
async def guard_redpanda_bus():
    from omnibase_infra.cli.delegate_lane_credentials import resolve_lane_binding
    from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
    from omnibase_infra.event_bus.models.config.model_kafka_event_bus_config import (
        ModelKafkaEventBusConfig,
    )

    from tests.bus_lane_guard import (
        assert_broker_in_declared_lane,
        declared_test_lane,
        resolve_test_bus_broker,
    )

    lane = declared_test_lane()
    selection, transport = resolve_lane_binding(lane, environ=dict(os.environ))
    broker = resolve_test_bus_broker()
    assert_broker_in_declared_lane(broker)
    # The same lane credential resolver as the canonical run-node caller. The
    # generic integration fixture defaults to plaintext and is not this overlay.
    username = (
        transport.sasl_username if transport else os.environ.get("KAFKA_SASL_USERNAME")
    )
    password = (
        transport.sasl_password.get_secret_value()
        if transport and transport.sasl_password
        else os.environ.get("KAFKA_SASL_PASSWORD")
    )
    config = ModelKafkaEventBusConfig(
        bootstrap_servers=broker,
        environment="integration-test",
        security_protocol=selection.security_protocol,
        sasl_mechanism=selection.sasl_mechanism,
        sasl_plain_username=username,
        sasl_plain_password=password,
        auto_offset_reset="earliest",
        enable_auto_commit=True,
    )
    bus = EventBusKafka(config=config)
    await bus.start()
    try:
        yield bus
    finally:
        await bus.close()


@pytest.mark.asyncio
async def test_unanswered_guard_redpanda_overlay(guard_redpanda_bus, tmp_path):
    raw = yaml.safe_load(CONTRACT.read_text())
    manifest = discover_contracts_from_paths([CONTRACT])
    assert not manifest.errors, manifest.errors
    engine = MessageDispatchEngine()
    report = await wire_from_manifest(
        ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
        engine,
        event_bus=guard_redpanda_bus,
        environment="integration-test",
    )
    assert {r.contract_name: r.outcome.value for r in report.results} == {
        raw["name"]: "wired"
    }
    engine.freeze()
    completed = asyncio.Queue()

    async def collect(message):
        await completed.put(json.loads(message.value))

    await guard_redpanda_bus.subscribe(
        raw["terminal_event"],
        group_id="guard-results-" + uuid4().hex,
        on_message=collect,
    )
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps(
            {"type": "user", "uuid": "u1", "message": {"content": "Please answer"}}
        )
        + "\n"
    )
    request = ModelUnansweredGuardRequest(
        session_id="lab-session",
        transcript_path=str(transcript),
        state_dir=str(tmp_path / "state"),
        environ={"ONEX_OPERATOR_MESSAGE_SPOOL": str(tmp_path / "spool")},
    )
    topic = raw["runtime_dispatch"]["command_topic"]
    # With no Part A binding the third turn may end only with a visible alarm,
    # never a fabricated claim or lane-start receipt.
    for expected in ["GateBlock", "GateBlock", "Alarm"]:
        command = ModelEventEnvelope[object](
            correlation_id=uuid4(),
            event_type=topic.removeprefix("onex.cmd.").removesuffix(".v1"),
            payload=request.model_dump(mode="json"),
        )
        await guard_redpanda_bus.publish(
            topic, None, command.model_dump_json().encode(), None
        )
        event = await asyncio.wait_for(completed.get(), timeout=30)
        assert event["correlation_id"] == str(command.correlation_id)
        assert event["payload"]["action"] == expected
        if expected == "Alarm":
            assert "ALARM" in event["payload"]["alarm"]
    transcript.write_text(
        transcript.read_text()
        + json.dumps({"type": "assistant", "message": {"content": "Answered"}})
        + "\n"
    )
    command = ModelEventEnvelope[object](
        correlation_id=uuid4(),
        event_type=topic.removeprefix("onex.cmd.").removesuffix(".v1"),
        payload=request.model_dump(mode="json"),
    )
    await guard_redpanda_bus.publish(
        topic, None, command.model_dump_json().encode(), None
    )
    event = await asyncio.wait_for(completed.get(), timeout=30)
    assert event["payload"]["action"] == "EndTurn"
