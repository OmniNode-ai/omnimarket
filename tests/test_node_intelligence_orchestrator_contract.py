# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

import importlib
from pathlib import Path

import yaml

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
        assert handler.get("name"), f"handler entry is missing name: {entry!r}"
        assert "function" not in handler

        module = importlib.import_module(handler["module"])
        handler_type = getattr(module, handler["name"])
        assert callable(handler_type)
        assert hasattr(handler_type(), "handle")
