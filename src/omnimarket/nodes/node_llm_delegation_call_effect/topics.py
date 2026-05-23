# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Topic constants for node_llm_delegation_call_effect, read from contract.yaml."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
    contract_subscribe_topics,
)

_CONTRACT = Path(__file__).parent / "contract.yaml"

_subscribe = contract_subscribe_topics(_CONTRACT)
_publish = contract_publish_topics(_CONTRACT)

TOPIC_DELEGATION_EXECUTE = _subscribe[0]

TOPIC_DELEGATION_CALL_COMPLETED = _publish[0]
TOPIC_DELEGATION_ESCALATION_TRIGGERED = _publish[1]
TOPIC_DELEGATION_ALL_TIERS_FAILED = _publish[2]
TOPIC_DELEGATION_MODEL_DEGRADED = _publish[3]

__all__ = [
    "TOPIC_DELEGATION_ALL_TIERS_FAILED",
    "TOPIC_DELEGATION_CALL_COMPLETED",
    "TOPIC_DELEGATION_ESCALATION_TRIGGERED",
    "TOPIC_DELEGATION_EXECUTE",
    "TOPIC_DELEGATION_MODEL_DEGRADED",
]
