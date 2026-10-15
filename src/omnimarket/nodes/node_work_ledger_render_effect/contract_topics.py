# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-derived topic constants for the markdown work-ledger projection (OMN-20275).

Every name here is READ OUT OF ``contract.yaml`` at import and none is spelled in this file.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.nodes.contract_topics import contract_publish_topics

CONTRACT_PATH = Path(__file__).resolve().parent / "contract.yaml"

_CONTRACT = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))

ALL_SUBSCRIBE_TOPICS: tuple[str, ...] = tuple(_CONTRACT["subscriptions"]["topics"])
if len(ALL_SUBSCRIBE_TOPICS) != 22:
    raise ValueError(
        "node_work_ledger_render_effect must subscribe to the eleven v1 and "
        f"eleven v2 row topics; found {len(ALL_SUBSCRIBE_TOPICS)}"
    )

_PUBLISH = contract_publish_topics(CONTRACT_PATH)
if len(_PUBLISH) != 2:
    raise ValueError(f"expected the rendered and blocked topics, found {_PUBLISH}")

TOPIC_RENDERED, TOPIC_BLOCKED = _PUBLISH

_DLQ = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))["event_bus"][
    "dlq_topics"
]
if len(_DLQ) != 1:
    raise ValueError(f"expected one malformed topic, found {_DLQ}")

TOPIC_MALFORMED = _DLQ[0]
