# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

import importlib
from pathlib import Path
from uuid import uuid4

import yaml
from omnibase_core.models.reducer.model_intent import ModelIntent
from omnibase_core.models.reducer.payloads.model_extension_payloads import (
    ModelPayloadExtension,
)

from omnimarket.nodes.node_intelligence_orchestrator.handlers.handler_receive_intent import (
    HandlerReceiveIntent,
    handle_receive_intent,
)

CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_intelligence_orchestrator"
    / "contract.yaml"
)


def _assert_runtime_routing_entry_shape(entry: dict[str, object]) -> None:
    handler = entry.get("handler")
    event_model = entry.get("event_model")

    assert isinstance(handler, dict), f"handler must be a mapping: {entry!r}"
    assert isinstance(entry.get("operation"), str)
    assert isinstance(handler.get("name"), str)
    assert isinstance(handler.get("module"), str)
    if event_model is not None:
        assert isinstance(event_model, dict), (
            f"event_model must be a mapping: {entry!r}"
        )
        assert isinstance(event_model.get("name"), str)
        assert isinstance(event_model.get("module"), str)
    if "event_type" in entry:
        assert isinstance(entry.get("event_type"), str)
    if "message_category" in entry:
        assert isinstance(entry.get("message_category"), str)


def test_intelligence_orchestrator_contract_declares_runtime_topics() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    event_bus = data["event_bus"]

    assert event_bus["subscribe_topics"] == [
        "onex.cmd.omnimarket.code-analysis.v1",
        "onex.cmd.omnimarket.document-ingestion.v1",
        "onex.cmd.omnimarket.pattern-learning.v1",
        "onex.cmd.omnimarket.intent-received.v1",
        "onex.evt.omnimarket.intent-drift-detected.v1",
    ]

    consumed_topics = {entry["topic"]: entry for entry in data["consumed_events"]}
    assert consumed_topics["onex.cmd.omnimarket.intent-received.v1"] == {
        "topic": "onex.cmd.omnimarket.intent-received.v1",
        "event_type": "IntentReceived",
        "schema_ref": "omnibase_core.models.reducer.model_intent.ModelIntent",
        "description": "Reducer intent received by the intelligence orchestrator",
    }


def test_intelligence_orchestrator_contract_uses_omnimarket_modules() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())

    assert data["input_model"]["module"] == (
        "omnimarket.nodes.node_intelligence_orchestrator.models"
    )
    assert data["output_model"]["module"] == (
        "omnimarket.nodes.node_intelligence_orchestrator.models"
    )

    subscribe_meta = data["event_bus"]["subscribe_topic_metadata"][
        "onex.cmd.omnimarket.code-analysis.v1"
    ]
    publish_meta = data["event_bus"]["publish_topic_metadata"][
        "onex.evt.omnimarket.code-analysis-completed.v1"
    ]

    assert subscribe_meta["schema_ref"].startswith(
        "omnimarket.nodes.node_intelligence_orchestrator.models."
    )
    assert publish_meta["schema_ref"].startswith(
        "omnimarket.nodes.node_intelligence_orchestrator.models."
    )

    drift_meta = data["event_bus"]["subscribe_topic_metadata"][
        "onex.evt.omnimarket.intent-drift-detected.v1"
    ]
    assert drift_meta["schema_ref"] == (
        "omnimarket.intelligence.events.ModelIntentDriftDetectedEnvelope"
    )


def test_intelligence_orchestrator_handler_routing_is_runtime_importable() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    handlers = data["handler_routing"]["handlers"]

    assert handlers
    for entry in handlers:
        handler = entry["handler"]
        _assert_runtime_routing_entry_shape(entry)
        name = handler.get("name")
        assert name, f"handler entry is missing name: {entry!r}"

        module = importlib.import_module(handler["module"])
        function = handler.get("function")
        handler_symbol = getattr(module, function or name)
        assert callable(handler_symbol)


def _make_intent(payload_data: dict[str, object]) -> ModelIntent:
    return ModelIntent(
        intent_type="extension",
        target="postgres://patterns/test-pattern",
        payload=ModelPayloadExtension(
            extension_type="omnimarket.pattern_lifecycle_update",
            plugin_name="omnimarket",
            data=payload_data,
        ),
    )


async def test_receive_intent_ignores_malformed_envelope_correlation_id() -> None:
    intent = _make_intent({"intent_type": "extension"})

    receipt = await HandlerReceiveIntent().handle(
        {
            "correlation_id": "not-a-uuid",
            "payload": intent,
        }
    )

    assert receipt.correlation_id is None


def test_receive_intent_ignores_malformed_payload_correlation_id() -> None:
    intent = _make_intent(
        {
            "intent_type": "extension",
            "correlation_id": "not-a-uuid",
        }
    )

    receipt = handle_receive_intent(intent)

    assert receipt.correlation_id is None


def test_receive_intent_preserves_valid_payload_correlation_id() -> None:
    correlation_id = uuid4()
    intent = _make_intent(
        {
            "intent_type": "extension",
            "correlation_id": str(correlation_id),
        }
    )

    receipt = handle_receive_intent(intent)

    assert receipt.correlation_id == correlation_id
