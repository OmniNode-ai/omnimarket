# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14494: node_projection_delegation_inference_response declares dlq_topics.

Before this fix, ``event_bus`` had ``subscribe_topics`` + ``publish_topics``
but NO ``dlq_topics`` -- a projection write error (the exact OMN-14487
``can't adapt type 'dict'`` crash, before it was fixed) was dropped to a
container ERROR log line and reached NO topic; a DLQ-only reaper missed it
entirely. This test proves the contract now declares a DLQ topic, matching
the four sibling projection nodes (node_projection_delegation,
node_projection_intent_classification, node_projection_live_events,
node_projection_savings) that already do.

RED before this fix: ``contract["event_bus"]["dlq_topics"]`` raised
``KeyError`` (the key did not exist). GREEN after: the key exists and names
exactly one durable failure-signal topic.
"""

from __future__ import annotations

from typing import Any

import yaml

_CONTRACT_PATH = (
    "src/omnimarket/nodes/node_projection_delegation_inference_response/contract.yaml"
)
_DLQ_TOPIC = "onex.dlq.omnimarket.projection-delegation-inference-response-malformed.v1"


def _load_contract() -> dict[str, Any]:
    with open(_CONTRACT_PATH) as f:
        contract: dict[str, Any] = yaml.safe_load(f)
        return contract


def test_contract_declares_dlq_topic() -> None:
    contract = _load_contract()
    dlq_topics = contract["event_bus"]["dlq_topics"]
    assert dlq_topics == [_DLQ_TOPIC]


def test_dlq_topic_follows_onex_dlq_naming_convention() -> None:
    contract = _load_contract()
    dlq_topics = contract["event_bus"]["dlq_topics"]
    for topic in dlq_topics:
        assert topic.startswith("onex.dlq.omnimarket.")
        assert topic.endswith("-malformed.v1")
