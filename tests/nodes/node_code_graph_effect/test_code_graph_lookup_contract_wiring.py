# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract gives the runtime a consumer for the lookup command and a topic for its answer (OMN-25354)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_code_graph_effect/contract.yaml"
)


def _contract() -> dict[str, object]:
    return yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))


def test_the_runtime_subscribes_to_the_lookup_command_topic() -> None:
    contract = _contract()
    topics = contract["topics"]
    assert isinstance(topics, dict)
    bus = contract["event_bus"]
    assert isinstance(bus, dict)
    assert bus["subscribe_topics"] == [topics["code_graph_lookup_requested"]]


def test_the_lookup_answer_is_published_on_the_answered_topic() -> None:
    contract = _contract()
    topics = contract["topics"]
    assert isinstance(topics, dict)
    bus = contract["event_bus"]
    assert isinstance(bus, dict)
    assert bus["publish_topics"] == [topics["code_graph_lookup_answered"]]


def test_the_lookup_operation_stays_bound_to_its_handler() -> None:
    contract = _contract()
    routing = contract["handler_routing"]
    assert isinstance(routing, dict)
    operations = {entry["operation"] for entry in routing["handlers"]}
    assert "code_graph_lookup" in operations


def test_the_answered_topic_is_the_terminal_event() -> None:
    contract = _contract()
    topics = contract["topics"]
    assert isinstance(topics, dict)
    assert contract["terminal_event"] == topics["code_graph_lookup_answered"]
