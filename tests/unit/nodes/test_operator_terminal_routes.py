# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Discovered operator handlers terminalize an execution failure on their failure topic."""

from __future__ import annotations

import asyncio
import importlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.topic_constants import derive_event_type_alias_for_topic
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

from omnimarket.models.model_operator_terminal_failure import (
    ModelOperatorTerminalFailure,
)
from omnimarket.models.model_orchestrator_promise import ModelPromiseEffectOverlay
from omnimarket.nodes.node_orchestrator_promise_capture_effect.handlers.store_promise_capture import (
    StorePromiseCapture,
)
from omnimarket.nodes.node_orchestrator_promise_classify_compute.node import (
    NodeOrchestratorPromiseClassifyCompute,
)

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("node_name", "success", "failure"),
    [
        (
            "node_ask_state_compute",
            "onex.evt.omnimarket.ask-state-completed.v1",
            "onex.evt.omnimarket.ask-state-failed.v1",
        ),
        (
            "node_operator_message_state_compute",
            "onex.evt.omnimarket.operator-message-state-completed.v1",
            "onex.evt.omnimarket.operator-message-state-failed.v1",
        ),
        (
            "node_operator_item_render_compute",
            "onex.evt.omnimarket.operator-item-render-completed.v1",
            "onex.evt.omnimarket.operator-item-render-failed.v1",
        ),
        (
            "node_ask_state_effect",
            "onex.evt.omnimarket.ask-carry-over-completed.v1",
            "onex.evt.omnimarket.ask-carry-over-failed.v1",
        ),
        (
            "node_orchestrator_promise_capture_effect",
            "onex.evt.omnimarket.orchestrator-promise-capture-completed.v1",
            "onex.evt.omnimarket.orchestrator-promise-capture-failed.v1",
        ),
        (
            "node_orchestrator_promise_classify_compute",
            "onex.evt.omnimarket.orchestrator-promise-classify-completed.v1",
            "onex.evt.omnimarket.orchestrator-promise-classify-failed.v1",
        ),
    ],
)
async def test_operator_failure_terminal_uses_discovered_bus_boundary(
    node_name, success, failure, tmp_path, monkeypatch
):
    contract_path = ROOT / "src/omnimarket/nodes" / node_name / "contract.yaml"
    raw = yaml.safe_load(contract_path.read_text())
    assert raw["terminal_event"] == success
    assert raw["runtime_dispatch"]["terminal_events"]["failure"] == failure
    base_module = importlib.import_module(raw["handler"]["module"])
    base_class = getattr(base_module, raw["handler"]["class"])

    def fail(self, request):
        raise OSError("injected private diagnostic that must stay off the bus")

    monkeypatch.setattr(base_class, "handle", fail)
    config = raw.get("config", {})
    policy = yaml.safe_load(
        (
            ROOT
            / "src/omnimarket/nodes/node_orchestrator_promise_classify_compute/contract.yaml"
        ).read_text()
    )["config"]
    utterance = {
        "capture_id": "terminal-test",
        "session_id": "session-a",
        "lane": "orchestrator-fixture",
        "text": "I will install it.",
        "source": "fixture",
        "captured_at": NOW.isoformat(),
    }
    if node_name == "node_ask_state_compute":
        payload = {
            "rows": [],
            "prompts": [],
            "now": NOW.isoformat(),
            "since": NOW.isoformat(),
            "config": config,
        }
    elif node_name == "node_operator_message_state_compute":
        payload = {"rows": [], "now": NOW.isoformat(), "config": config}
    elif node_name == "node_operator_item_render_compute":
        payload = {"now": NOW.isoformat()}
    elif node_name == "node_ask_state_effect":
        payload = {
            "read": {
                "captures_dir": str(tmp_path),
                "ledger_path": str(tmp_path / "ledger"),
                "since": NOW.isoformat(),
                "now": NOW.isoformat(),
                "archives": False,
            },
            "ask_ids": ["ask-a"],
        }
    elif node_name == "node_orchestrator_promise_capture_effect":
        payload = {
            "session_id": "session-a",
            "lane": "orchestrator-fixture",
            "content_kind": "assistant_reply",
            "content_record_id": "terminal-test",
            "emitted_at": NOW.isoformat(),
            "content": utterance["text"],
        }
    else:
        payload = {"utterance": utterance, "policy": policy}
    store = StorePromiseCapture(tmp_path / "promises.sqlite")
    overlay = ModelPromiseEffectOverlay(
        store_path=str(tmp_path / "promises.sqlite"),
        assistant_reply_kind="assistant_reply",
        policy=policy,
    )
    bus = EventBusInmemory(environment="local", group="operator-failure-test")
    received = asyncio.Queue()
    await bus.start()

    async def collect(message):
        await received.put((message.topic, json.loads(message.value)))

    try:
        for topic in (success, failure):
            await bus.subscribe(
                topic, group_id="operator-failure-results", on_message=collect
            )
        discovered = discover_contracts_from_paths([contract_path])
        assert not discovered.errors
        engine = MessageDispatchEngine()
        wiring = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=tuple(discovered.contracts)),
            engine,
            event_bus=bus,
            environment="local",
            materialized_explicit_dependencies={
                "HandlerPromiseCapture": {
                    "store": store,
                    "classifier": NodeOrchestratorPromiseClassifyCompute(),
                    "overlay": overlay,
                }
            },
        )
        assert [result.outcome.value for result in wiring.results] == ["wired"]
        engine.freeze()
        command = raw["runtime_dispatch"]["command_topic"]
        correlation = uuid4()
        envelope = ModelEventEnvelope[object](
            payload=payload,
            correlation_id=correlation,
            event_type=derive_event_type_alias_for_topic(command),
        )
        await bus.publish(command, None, envelope.model_dump_json().encode(), None)
        topic, event = await asyncio.wait_for(received.get(), timeout=5)
        assert topic == failure
        assert event["correlation_id"] == str(correlation)
        assert event["payload"] == ModelOperatorTerminalFailure(
            terminal_failure_cause="OSError"
        ).model_dump(mode="json")
        assert received.empty()
    finally:
        await bus.close()
        store.close()
