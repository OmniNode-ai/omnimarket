# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Installed discovery and production dispatch wiring, with injected effect dependencies."""

import asyncio
import json
from datetime import UTC, datetime
from importlib.resources import files
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

from omnimarket.models.model_orchestrator_promise import (
    ModelPromiseBarRequest,
    ModelPromiseCaptureRecord,
    ModelPromiseClassifyRequest,
    ModelPromiseEffectOverlay,
    ModelPromisePolicy,
    ModelPromiseRenderRequest,
)
from omnimarket.models.operator_capture import ModelUtterance
from omnimarket.nodes.node_orchestrator_promise_capture_effect.handlers.store_promise_capture import (
    StorePromiseCapture,
)
from omnimarket.nodes.node_orchestrator_promise_classify_compute.node import (
    NodeOrchestratorPromiseClassifyCompute,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("overlay_state", ["missing", "invalid", "configured"])
async def test_promise_capture_auto_wires_before_resolving_deployment_overlay(
    overlay_state, tmp_path, monkeypatch
) -> None:
    node_name = "node_orchestrator_promise_capture_effect"
    raw = contract(node_name)
    policy = contract("node_orchestrator_promise_classify_compute")["config"]
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    if overlay_state != "missing":
        overlay_dir = tmp_path / node_name
        overlay_dir.mkdir()
        document = (
            {"store_path": str(tmp_path / "capture.sqlite")}
            if overlay_state == "invalid"
            else ModelPromiseEffectOverlay(
                store_path=str(tmp_path / "capture.sqlite"),
                assistant_reply_kind="assistant_reply",
                policy=policy,
            ).model_dump(mode="json")
        )
        (overlay_dir / "overlay.yaml").write_text(yaml.safe_dump(document))
    discovered = tuple(c for c in discover_contracts().contracts if c.name == node_name)
    bus = EventBusInmemory(environment="local", group="promise-overlay-runtime")
    received = asyncio.Queue()
    await bus.start()

    async def collect(message):
        await received.put((message.topic, json.loads(message.value)))

    try:
        for topic in raw["event_bus"]["publish_topics"]:
            await bus.subscribe(topic, group_id="overlay-results", on_message=collect)
        engine = MessageDispatchEngine()
        report = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=discovered),
            engine,
            event_bus=bus,
            environment="local",
        )
        assert [result.outcome.value for result in report.results] == ["wired"]
        engine.freeze()
        command = raw["runtime_dispatch"]["command_topic"]
        record = ModelPromiseCaptureRecord(
            session_id="session-a",
            lane="orchestrator-fixture",
            content_kind="assistant_reply",
            content_record_id="overlay-runtime",
            emitted_at="2026-11-04T12:00:00Z",
            content="I will install it. Thanks.",
        )
        for replay in range(2):
            envelope = ModelEventEnvelope[object](
                payload=record.model_dump(mode="json"),
                event_type=command,
                correlation_id=uuid4(),
            )
            await bus.publish(command, None, envelope.model_dump_json().encode(), None)
            topic, event = await asyncio.wait_for(received.get(), timeout=5)
            assert event["correlation_id"] == str(envelope.correlation_id)
            if overlay_state == "configured":
                assert topic == raw["terminal_event"]
                assert event["payload"]["status"] == (
                    "recorded" if replay == 0 else "duplicate"
                )
                assert event["payload"]["candidate_count"] == 1
            else:
                assert topic == raw["runtime_dispatch"]["terminal_events"]["failure"]
                assert event["payload"] == {
                    "status": "failed",
                    "terminal_failure_cause": "NodeOverlayError",
                }
        assert received.empty()
    finally:
        await bus.close()


def contract(name):
    return yaml.safe_load(
        files("omnimarket.nodes." + name).joinpath("contract.yaml").read_text()
    )


@pytest.mark.parametrize(
    "node_name",
    [
        "node_orchestrator_promise_classify_compute",
        "node_orchestrator_promise_capture_effect",
    ],
)
async def test_promise_classify_runtime_discovers_and_dispatches(
    node_name, tmp_path
) -> None:
    raw = contract(node_name)
    policy = ModelPromisePolicy.model_validate(
        contract("node_orchestrator_promise_classify_compute")["config"]
    )
    store = StorePromiseCapture(tmp_path / "capture.sqlite")
    overlay = ModelPromiseEffectOverlay(
        store_path=str(tmp_path / "capture.sqlite"),
        assistant_reply_kind="assistant_reply",
        policy=policy,
    )
    discovered = tuple(c for c in discover_contracts().contracts if c.name == node_name)
    assert len(discovered) == 1
    command = raw["runtime_dispatch"]["command_topic"]
    terminal = raw["terminal_event"]
    bus = EventBusInmemory(environment="local", group="promise-runtime")
    received = asyncio.Queue()
    await bus.start()

    async def collect(message):
        await received.put(json.loads(message.value))

    try:
        await bus.subscribe(terminal, group_id="promise-results", on_message=collect)
        engine = MessageDispatchEngine()
        report = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=discovered),
            engine,
            event_bus=bus,
            environment="local",
            materialized_explicit_dependencies={
                "HandlerPromiseCapture": {
                    "store": store,
                    "classifier": NodeOrchestratorPromiseClassifyCompute(),
                    "overlay": overlay,
                },
            },
        )
        assert [item.outcome.value for item in report.results] == ["wired"]
        assert command in report.results[0].topics_subscribed
        engine.freeze()
        record = ModelPromiseCaptureRecord(
            session_id="session-a",
            lane="orchestrator-fixture",
            content_kind="assistant_reply",
            content_record_id="stop-runtime",
            emitted_at="2026-11-04T12:00:00Z",
            content="I will install it. Thanks.",
        )
        request = (
            record
            if node_name.endswith("_effect")
            else ModelPromiseClassifyRequest(
                utterance=ModelUtterance(
                    capture_id="stop-runtime",
                    session_id=record.session_id,
                    lane=record.lane,
                    text=record.content,
                    source="fixture",
                    captured_at=datetime(2026, 11, 4, 12, tzinfo=UTC),
                ),
                policy=policy,
            )
        )
        for replay in range(2):
            correlation = uuid4()
            envelope = ModelEventEnvelope[object](
                payload=request.model_dump(mode="json"),
                correlation_id=correlation,
                event_type=command,
            )
            await bus.publish(command, None, envelope.model_dump_json().encode(), None)
            event = await asyncio.wait_for(received.get(), timeout=10)
            assert event["correlation_id"] == str(correlation)
            payload = event["payload"]
            if node_name.endswith("_effect"):
                assert payload["candidate_count"] == 1
                assert payload["ledger_rows"] == []
                assert payload["status"] == ("recorded" if replay == 0 else "duplicate")
                assert len(payload["rows"]) == (3 if replay == 0 else 0)
            else:
                assert payload["items"][0]["kind"] == "promise"
                assert payload["items"][0]["state"] == "candidate"
                assert len(payload["dispositions"]) == 2
        if node_name.endswith("_compute"):
            render_topic = next(
                route["topic"]
                for route in raw["handler_routing"]["handlers"]
                if route["operation"] == "render_promises"
            )
            render = ModelPromiseRenderRequest(
                captures=(), now=datetime(2026, 11, 4, 13, tzinfo=UTC)
            )
            envelope = ModelEventEnvelope[object](
                payload=render.model_dump(mode="json"), event_type=render_topic
            )
            await bus.publish(
                render_topic, None, envelope.model_dump_json().encode(), None
            )
            event = await asyncio.wait_for(received.get(), timeout=10)
            assert event["payload"]["candidate_count"] == 0
            bar_topic = next(
                route["topic"]
                for route in raw["handler_routing"]["handlers"]
                if route["operation"] == "measure_promise_bar"
            )
            bar = ModelPromiseBarRequest(
                corpus_id="runtime-fixture",
                origin="synthetic",
                labelled_by="runtime-fixture",
                samples=(),
                policy=policy,
            )
            envelope = ModelEventEnvelope[object](
                payload=bar.model_dump(mode="json"),
                event_type=bar_topic,
                correlation_id=uuid4(),
            )
            await bus.publish(
                bar_topic, None, envelope.model_dump_json().encode(), None
            )
            event = await asyncio.wait_for(received.get(), timeout=10)
            assert event["correlation_id"] == str(envelope.correlation_id)
            assert event["payload"]["replies"] == 0
            assert event["payload"]["passed"] is False
    finally:
        await bus.close()
        store.close()
