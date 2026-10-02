# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-derived topic constants for the work-ledger projection (OMN-19513).

Named ``contract_topics.py``, never ``topics.py``: every name here is READ OUT OF
``contract.yaml`` at import and none is spelled in this file.
"""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
    contract_subscribe_topics,
)

CONTRACT_PATH = Path(__file__).resolve().parent / "contract.yaml"

ALL_SUBSCRIBE_TOPICS: tuple[str, ...] = contract_subscribe_topics(CONTRACT_PATH)
SUBSCRIBE_TOPICS = tuple(
    topic for topic in ALL_SUBSCRIBE_TOPICS if topic.endswith(".v1")
)
TYPED_SUBSCRIBE_TOPICS = tuple(
    topic for topic in ALL_SUBSCRIBE_TOPICS if topic.endswith(".v2")
)
if len(SUBSCRIBE_TOPICS) != 11:
    raise ValueError(
        "node_projection_work_ledger must subscribe to exactly the eleven "
        f"work-ledger row topics; found {len(SUBSCRIBE_TOPICS)}"
    )

if len(TYPED_SUBSCRIBE_TOPICS) != 11 or len(ALL_SUBSCRIBE_TOPICS) != 22:
    raise ValueError("work-ledger requires the exact eleven v1 and eleven v2 routes")

_PUBLISH = contract_publish_topics(CONTRACT_PATH)
if len(_PUBLISH) != 1:
    raise ValueError(f"expected one applied topic, found {len(_PUBLISH)}: {_PUBLISH}")

TOPIC_PROJECTION_APPLIED = _PUBLISH[0]

__all__: list[str] = [
    "ALL_SUBSCRIBE_TOPICS",
    "CONTRACT_PATH",
    "SUBSCRIBE_TOPICS",
    "TOPIC_PROJECTION_APPLIED",
    "TYPED_SUBSCRIBE_TOPICS",
]
