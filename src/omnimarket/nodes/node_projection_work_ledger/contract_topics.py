# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-derived topic constants for the work-ledger projection (OMN-19513).

Named ``contract_topics.py``, never ``topics.py``: every name here is READ OUT OF
``contract.yaml`` at import and none is spelled in this file.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
    contract_subscribe_topics,
)

CONTRACT_PATH = Path(__file__).resolve().parent / "contract.yaml"

ALL_SUBSCRIBE_TOPICS: tuple[str, ...] = contract_subscribe_topics(CONTRACT_PATH)

# OMN-25630, L12: the 22 old row topics are the rollback path, never a subscription. The republish command
# writes back to them; the writer still folds a message from one of them if a rollback re-subscribes it.
ROLLBACK_ROW_TOPICS: tuple[str, ...] = tuple(
    yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))["rollback"][
        "republish_old_topics"
    ]
)
SUBSCRIBE_TOPICS = tuple(t for t in ROLLBACK_ROW_TOPICS if t.endswith(".v1"))
TYPED_SUBSCRIBE_TOPICS = tuple(t for t in ROLLBACK_ROW_TOPICS if t.endswith(".v2"))
if len(SUBSCRIBE_TOPICS) != 11 or len(TYPED_SUBSCRIBE_TOPICS) != 11:
    raise ValueError(
        "work-ledger rollback requires the exact eleven v1 and eleven v2 routes"
    )
if len(set(ROLLBACK_ROW_TOPICS)) != 22:
    raise ValueError("the 22 rollback row topics must be distinct")

_PUBLISH = contract_publish_topics(CONTRACT_PATH)
_APPLIED = tuple(t for t in _PUBLISH if "projection-work-ledger-applied" in t)
_ACCEPTED_ROW = tuple(t for t in _PUBLISH if "work-ledger-row-accepted" in t)
if len(_APPLIED) != 1 or len(_ACCEPTED_ROW) != 1 or len(_PUBLISH) != 2:
    raise ValueError(
        f"expected the applied topic and the accepted-row topic, found {_PUBLISH}"
    )

TOPIC_PROJECTION_APPLIED = _APPLIED[0]
TOPIC_ACCEPTED_ROW = _ACCEPTED_ROW[0]
if ALL_SUBSCRIBE_TOPICS != (TOPIC_ACCEPTED_ROW,):
    raise ValueError(
        f"node_projection_work_ledger subscribes to the accepted-row topic alone, found {ALL_SUBSCRIBE_TOPICS}"
    )

__all__: list[str] = [
    "ALL_SUBSCRIBE_TOPICS",
    "CONTRACT_PATH",
    "ROLLBACK_ROW_TOPICS",
    "SUBSCRIBE_TOPICS",
    "TOPIC_ACCEPTED_ROW",
    "TOPIC_PROJECTION_APPLIED",
    "TYPED_SUBSCRIBE_TOPICS",
]
