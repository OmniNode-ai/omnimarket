# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stall responder's command topic has a producer that runs on the runtime tick."""

from __future__ import annotations

import pytest

from tests.nodes.trigger_declaration_support import (
    command_topic,
    load_contract,
    publishing_nodes,
    subscribes_to_runtime_tick,
)

pytestmark = pytest.mark.unit

NODE = "node_stall_responder_compute"


def test_the_command_topic_has_a_producer_node() -> None:
    topic = command_topic(load_contract(NODE))
    producers = publishing_nodes(topic)
    assert producers, (
        f"{NODE} subscribes only to {topic} and no node contract publishes it: "
        "declare the producer or mark the node CI-only"
    )


def test_a_producer_of_the_command_topic_is_scheduled_on_the_runtime_tick() -> None:
    topic = command_topic(load_contract(NODE))
    scheduled = [
        name
        for name in publishing_nodes(topic)
        if subscribes_to_runtime_tick(load_contract(name))
    ]
    assert scheduled, f"no producer of {topic} subscribes to the runtime tick"
