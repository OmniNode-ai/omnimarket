# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

import importlib
from pathlib import Path

import yaml
from omnibase_infra.runtime.auto_wiring.models import ModelHandlerRoutingEntry

CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_quality_scoring_compute"
    / "contract.yaml"
)


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
        runtime_entry = {
            key: entry[key]
            for key in (
                "handler",
                "event_model",
                "operation",
                "event_type",
                "message_category",
            )
            if key in entry
        }
        ModelHandlerRoutingEntry.model_validate(runtime_entry)

        handler = entry["handler"]
        assert handler.get("name"), f"handler entry is missing name: {entry!r}"
        assert "function" not in handler

        module = importlib.import_module(handler["module"])
        handler_type = getattr(module, handler["name"])
        assert callable(handler_type)
        assert hasattr(handler_type(), "handle")
