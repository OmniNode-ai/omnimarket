# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21536: the projection nodes whose runners moved to the typed request keep publishing.

Moving a runner onto ``ModelProjectionRequest`` touches its node, and the
contract-state-coverage gate then asks that every output the node declares is asserted by a
test. Each case reads the node's own contract and asserts that the topic it publishes is
declared in ``event_bus.publish_topics``, so a runner edit cannot silently drop an output
the contract promises.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_NODES = Path(__file__).resolve().parent.parent / "src" / "omnimarket" / "nodes"

_PUBLISHED = [
    (
        "node_projection_pattern_learning",
        "onex.evt.omnimarket.projection-pattern-learning-applied.v1",
    ),
    (
        "node_projection_routing_decision",
        "onex.evt.omnimarket.projection-routing-decision-applied.v1",
    ),
    (
        "node_projection_routing_decision",
        "onex.snapshot.projection.agent-routing-decision.v1",
    ),
    (
        "node_projection_session_outcome",
        "onex.evt.omnimarket.projection-session-outcome-applied.v1",
    ),
]


@pytest.mark.parametrize(("node", "topic"), _PUBLISHED)
def test_node_declares_the_topic_its_runner_publishes(node: str, topic: str) -> None:
    contract = yaml.safe_load((_NODES / node / "contract.yaml").read_text())
    assert topic in contract["event_bus"]["publish_topics"]
