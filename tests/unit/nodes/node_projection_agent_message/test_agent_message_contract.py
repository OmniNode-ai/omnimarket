# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The projection node reads the declared agent message topic, exposes its table through the projection API, and holds no topic in code."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_projection_agent_message.handlers.handler_agent_message_writer import (
    AgentMessageRowWriter,
)
from omnimarket.projection.discovery import build_projection_topic_map

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
NODE = ROOT / "src/omnimarket/nodes/node_projection_agent_message"
TOPIC = "onex.evt.omnimarket.agent-message.v1"


def _contract() -> dict[str, object]:
    loaded = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_the_node_subscribes_to_the_agent_message_topic_only() -> None:
    event_bus = _contract()["event_bus"]
    assert isinstance(event_bus, dict)
    assert event_bus["subscribe_topics"] == [TOPIC]


def test_the_topic_is_the_one_the_sender_node_publishes() -> None:
    sender = yaml.safe_load(
        (
            ROOT / "src/omnimarket/nodes/node_agent_message_effect/contract.yaml"
        ).read_text()
    )
    assert sender["event_bus"]["publish_topics"] == [TOPIC]


def test_the_writer_reads_its_topic_from_the_contract() -> None:
    assert AgentMessageRowWriter().subscribe_topics == [TOPIC]


def test_the_table_is_served_by_the_projection_api_with_its_cached_read() -> None:
    topic_map = build_projection_topic_map()
    served = [c for c in topic_map.values() if c.table == "agent_messages"]
    assert len(served) == 1
    assert "sender_verified" in served[0].columns
    assert "refusal_reason" in served[0].columns


def test_the_contract_declares_the_sender_proof_as_not_deployed() -> None:
    proof = _contract()["sender_proof"]
    assert isinstance(proof, dict)
    assert proof["deployed"] is False
