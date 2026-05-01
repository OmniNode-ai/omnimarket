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
    / "node_quality_scoring_compute"
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


def test_quality_scoring_contract_declares_runtime_topics() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    event_bus = data["event_bus"]

    assert event_bus["subscribe_topics"] == [
        "onex.cmd.omnimarket.quality-assessment.v1"
    ]
    assert event_bus["publish_topics"] == [
        "onex.evt.omnimarket.quality-assessment-completed.v1"
    ]


def test_quality_scoring_contract_uses_omnimarket_models() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    event_bus = data["event_bus"]

    subscribe_meta = event_bus["subscribe_topic_metadata"][
        "onex.cmd.omnimarket.quality-assessment.v1"
    ]
    publish_meta = event_bus["publish_topic_metadata"][
        "onex.evt.omnimarket.quality-assessment-completed.v1"
    ]

    assert subscribe_meta["schema_ref"].startswith(
        "omnimarket.nodes.node_quality_scoring_compute.models."
    )
    assert publish_meta["schema_ref"].startswith(
        "omnimarket.nodes.node_quality_scoring_compute.models."
    )


def test_quality_scoring_contract_is_intelligence_runtime_owned() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())

    assert data["runtime_profiles"] == ["intelligence"]
    assert "main" not in data["runtime_profiles"]


def test_quality_scoring_handler_routing_is_runtime_importable() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    handlers = data["handler_routing"]["handlers"]

    assert handlers
    for entry in handlers:
        _assert_runtime_routing_entry_shape(entry)

        handler = entry["handler"]
        assert handler.get("name"), f"handler entry is missing name: {entry!r}"
        assert "function" not in handler

        module = importlib.import_module(handler["module"])
        handler_type = getattr(module, handler["name"])
        assert callable(handler_type)
        assert hasattr(handler_type(), "handle")
