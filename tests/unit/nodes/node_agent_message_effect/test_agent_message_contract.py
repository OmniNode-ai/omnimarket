# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node contract and the emit daemon's topic registry declare the same one topic."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_event_emit_effect.spool.topic_resolver import (
    EnumDurabilityTier,
    resolve_event_type,
    resolve_partition_key_field,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
NODE = ROOT / "src/omnimarket/nodes/node_agent_message_effect"
TOPIC = "onex.evt.omnimarket.agent-message.v1"
EVENT_TYPE = "agent.message"


def _contract() -> dict[str, object]:
    loaded = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_the_contract_declares_the_topic_it_publishes() -> None:
    event_bus = _contract()["event_bus"]
    assert isinstance(event_bus, dict)
    assert event_bus["publish_topics"] == [TOPIC]


def test_the_contract_declares_the_topic_it_reads() -> None:
    reads = _contract()["caller_subscription"]
    assert isinstance(reads, dict)
    assert reads["topics"] == [TOPIC]
    assert reads["group_id_prefix"] == "agent-message-read"


def test_the_registry_declares_the_same_topic_as_duty_critical() -> None:
    resolved = resolve_event_type(EVENT_TYPE)
    assert [(r.topic, r.tier) for r in resolved] == [
        (TOPIC, EnumDurabilityTier.DUTY_CRITICAL)
    ]
    assert resolve_partition_key_field(EVENT_TYPE) == "thread"


def test_the_contract_and_the_registry_topics_are_equal() -> None:
    event_bus = _contract()["event_bus"]
    assert isinstance(event_bus, dict)
    declared = set(event_bus["publish_topics"])
    registered = {r.topic for r in resolve_event_type(EVENT_TYPE)}
    assert declared == registered


def test_the_contract_names_an_effect_node_with_its_handler() -> None:
    contract = _contract()
    assert contract["node_type"] == "EFFECT_GENERIC"
    handler = contract["handler"]
    assert isinstance(handler, dict)
    assert handler["class"] == "HandlerAgentMessage"
    assert handler["module"].endswith("handlers.handler_agent_message")  # type: ignore[union-attr]


def test_no_handler_spells_the_topic() -> None:
    for path in (NODE / "handlers").glob("*.py"):
        assert TOPIC not in path.read_text(), path.name


def test_the_node_has_metadata() -> None:
    metadata = yaml.safe_load((NODE / "metadata.yaml").read_text())
    assert metadata["name"] == "node_agent_message_effect"
    assert metadata["node_role"] == "effect"
