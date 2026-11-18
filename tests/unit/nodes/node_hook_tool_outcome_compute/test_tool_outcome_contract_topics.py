# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam test: the topics this node's contract names, against the producer side."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import yaml

NODES = Path(__file__).resolve().parents[4] / "src" / "omnimarket" / "nodes"
CONTRACT = NODES / "node_hook_tool_outcome_compute" / "contract.yaml"
EMIT_REGISTRY = NODES / "node_emit_daemon" / "registries" / "topics.yaml"

CAPTURE_TOPIC = "onex.cmd.omniintelligence.content-captured.v1"
OUTCOME_TOPIC = "onex.evt.omnimarket.hook-tool-outcome-classified.v1"


def _contract() -> dict[str, Any]:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _fan_out_topics(registry: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    for entry in registry.values():
        if isinstance(entry, dict):
            for fan in entry.get("fan_out", []):
                if isinstance(fan, dict) and "topic" in fan:
                    found.add(str(fan["topic"]))
    return found


def _registry_entries() -> dict[str, Any]:
    loaded = yaml.safe_load(EMIT_REGISTRY.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    for value in loaded.values():
        if isinstance(value, dict) and "content.captured" in value:
            return value
    return loaded


def test_contract_subscribes_to_the_capture_topic_and_publishes_the_outcome() -> None:
    bus = _contract()["event_bus"]
    assert bus["subscribe_topics"] == [CAPTURE_TOPIC]
    assert bus["publish_topics"] == [OUTCOME_TOPIC]
    assert _contract()["terminal_event"] == OUTCOME_TOPIC


def test_subscribed_topic_is_fanned_out_by_the_capture_producer() -> None:
    """The producer side of the seam: the emit registry fans content out to it."""
    entries = _registry_entries()
    assert "content.captured" in entries
    assert CAPTURE_TOPIC in _fan_out_topics(
        {"content.captured": entries["content.captured"]}
    )


def test_the_published_topic_is_declared_as_a_sink() -> None:
    assert OUTCOME_TOPIC in _contract()["externally_consumed_topics"]


def test_handler_and_routing_resolve_to_real_objects() -> None:
    contract = _contract()
    handler = contract["handler"]
    handler_cls = getattr(importlib.import_module(handler["module"]), handler["class"])
    routed = contract["handler_routing"]["handlers"][0]
    assert routed["handler"]["name"] == handler["class"] == handler_cls.__name__
    event_model = routed["event_model"]
    model = getattr(importlib.import_module(event_model["module"]), event_model["name"])
    assert contract["input_model"]["name"] == model.__name__
    output = contract["output_model"]
    getattr(importlib.import_module(output["module"]), output["name"])


def test_runtime_profile_is_a_consumer_attached_profile() -> None:
    contract = _contract()
    assert contract["runtime_profiles"] == ["main"]
    assert contract["descriptor"]["runtime_profiles"] == ["main"]
