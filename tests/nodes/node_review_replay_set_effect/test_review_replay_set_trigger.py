# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The review replay set's freeze command names its producer (on demand by design, never scheduled)."""

from __future__ import annotations

import pytest

from tests.nodes.trigger_declaration_support import (
    command_topic,
    load_contract,
    named_external_producer,
    publishing_nodes,
)

pytestmark = pytest.mark.unit

NODE = "node_review_replay_set_effect"
FREEZE_TOPIC = "onex.cmd.omnimarket.review-replay-set-freeze-requested.v1"


def test_review_replay_set_freeze_command_topic_is_the_declared_one() -> None:
    assert command_topic(load_contract(NODE)) == FREEZE_TOPIC


def test_review_replay_set_freeze_command_has_a_declared_producer() -> None:
    contract = load_contract(NODE)
    topic = command_topic(contract)
    producer = named_external_producer(contract, topic)
    assert producer or publishing_nodes(topic), (
        f"{NODE} subscribes to {topic} and declares no producer of it"
    )


def test_review_replay_set_producer_is_the_operator_dispatch_and_not_a_schedule() -> (
    None
):
    contract = load_contract(NODE)
    producer = named_external_producer(contract, command_topic(contract))
    assert producer is not None
    assert "onex run-node" in producer
    assert "schedule" not in contract
