# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-derived projection topics (OMN-19999)."""

from pathlib import Path

from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
    contract_subscribe_topics,
)

CONTRACT_PATH = Path(__file__).resolve().parent / "contract.yaml"
SUBSCRIBE_TOPICS = contract_subscribe_topics(CONTRACT_PATH)
if len(SUBSCRIBE_TOPICS) != 1:
    raise ValueError("PR state requires exactly one observation topic")
_PUBLISH = contract_publish_topics(CONTRACT_PATH)
if len(_PUBLISH) != 1:
    raise ValueError("PR state requires exactly one applied topic")
TOPIC_PROJECTION_APPLIED = _PUBLISH[0]
