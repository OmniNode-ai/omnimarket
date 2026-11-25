# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Publish the owner-missing event on the topic this node's contract declares (OMN-26205).

The event goes through node_event_emit_effect, which spools it to disk before it publishes, so an event made while no broker
is reachable is delivered on the effect's next invocation. The event id is the item id: a consumer deduplicates by it.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from omnimarket.events.emit_effect_topic_publisher import EmitEffectTopicPublisher
from omnimarket.models.dropped_work_audit import ModelOwnerMissingEvent

_CONTRACT = Path(__file__).resolve().parent.parent / "contract.yaml"


class OwnerMissingTopicError(RuntimeError):
    """The contract declares no owner-missing topic."""


def owner_missing_topic() -> str:
    """The topic of ``topics.owner_missing`` in this node's contract; no topic string is held in code."""
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    topic = (contract.get("topics") or {}).get("owner_missing")
    if not isinstance(topic, str) or not topic:
        raise OwnerMissingTopicError("the contract declares no topics.owner_missing")
    return topic


class EmitOwnerMissingPublisher:
    """Publish one owner-missing event through the emit effect."""

    def __init__(self, topic: str | None = None) -> None:
        self._topic = topic if topic is not None else owner_missing_topic()
        self._emit = EmitEffectTopicPublisher()

    def publish(self, event: ModelOwnerMissingEvent) -> bool:
        return self._emit.publish(
            topic=self._topic,
            event_type=type(event).__name__,
            payload=json.loads(event.model_dump_json()),
            event_id=event.event_id,
            partition_key=event.event_id,
        )


__all__: list[str] = [
    "EmitOwnerMissingPublisher",
    "OwnerMissingTopicError",
    "owner_missing_topic",
]
