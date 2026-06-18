# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-backed topic constants for the delegation orchestrator.

OMN-13193 (Workstream A, phase A3): every topic the orchestrator publishes or
subscribes to is resolved from this node's ``contract.yaml`` via the adopted
``contract_publish_topics`` / ``contract_subscribe_topics`` helpers, rather than
imported from the infra event-bus topic constants. The contract is the single
source of truth (OMN-12803); the Python module is a thin binding layer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
    contract_subscribe_topics,
)

_CONTRACT_PATH: Final[Path] = Path(__file__).parent / "contract.yaml"


def _single_topic(
    topics: tuple[str, ...],
    fragment: str,
    *,
    section: str,
) -> str:
    """Return the one contract topic in *topics* containing *fragment*.

    Fails fast if the fragment matches zero or more than one declared topic so
    a contract edit that drops or duplicates a topic surfaces at import time.
    """
    matches = tuple(topic for topic in topics if fragment in topic)
    if len(matches) != 1:
        raise ValueError(
            f"{_CONTRACT_PATH} expected exactly one event_bus.{section} topic "
            f"containing {fragment!r}; found {matches!r}"
        )
    return matches[0]


_SUBSCRIBE_TOPICS: Final[tuple[str, ...]] = contract_subscribe_topics(_CONTRACT_PATH)
_PUBLISH_TOPICS: Final[tuple[str, ...]] = contract_publish_topics(_CONTRACT_PATH)

# Subscribe topics (commands/events the orchestrator consumes).
TOPIC_ID_DELEGATION_REQUEST: Final[str] = _single_topic(
    _SUBSCRIBE_TOPICS, "delegation-request.v1", section="subscribe_topics"
)
TOPIC_ID_INVOCATION_COMMAND: Final[str] = _single_topic(
    _SUBSCRIBE_TOPICS, "invocation.v1", section="subscribe_topics"
)
TOPIC_ID_AGENT_TASK_LIFECYCLE: Final[str] = _single_topic(
    _SUBSCRIBE_TOPICS, "agent-task-lifecycle.v1", section="subscribe_topics"
)
TOPIC_ID_INFERENCE_RESPONSE: Final[str] = _single_topic(
    _SUBSCRIBE_TOPICS, "inference-response.v1", section="subscribe_topics"
)

# Publish topics (commands/events the orchestrator emits).
TOPIC_ID_INFERENCE_REQUEST: Final[str] = _single_topic(
    _PUBLISH_TOPICS, "delegation-inference-request.v1", section="publish_topics"
)
TOPIC_ID_QUALITY_GATE_REQUEST: Final[str] = _single_topic(
    _PUBLISH_TOPICS, "delegation-quality-gate-request.v1", section="publish_topics"
)
TOPIC_ID_ROUTING_REQUEST: Final[str] = _single_topic(
    _PUBLISH_TOPICS, "delegation-routing-request.v1", section="publish_topics"
)
TOPIC_ID_DELEGATION_COMPLETED: Final[str] = _single_topic(
    _PUBLISH_TOPICS, "delegation-completed.v1", section="publish_topics"
)
TOPIC_ID_DELEGATION_FAILED: Final[str] = _single_topic(
    _PUBLISH_TOPICS, "delegation-failed.v1", section="publish_topics"
)
TOPIC_ID_TASK_DELEGATED: Final[str] = _single_topic(
    _PUBLISH_TOPICS, "task-delegated.v1", section="publish_topics"
)

__all__ = [
    "TOPIC_ID_AGENT_TASK_LIFECYCLE",
    "TOPIC_ID_DELEGATION_COMPLETED",
    "TOPIC_ID_DELEGATION_FAILED",
    "TOPIC_ID_DELEGATION_REQUEST",
    "TOPIC_ID_INFERENCE_REQUEST",
    "TOPIC_ID_INFERENCE_RESPONSE",
    "TOPIC_ID_INVOCATION_COMMAND",
    "TOPIC_ID_QUALITY_GATE_REQUEST",
    "TOPIC_ID_ROUTING_REQUEST",
    "TOPIC_ID_TASK_DELEGATED",
]
