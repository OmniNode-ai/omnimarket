# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures of the broker topic removal (OMN-23697): the overlay from its example, and a
World of the broker's topics, the contracts' declarations and a remover, each breakable by name.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml

from omnimarket.events.topic_removal import (
    ModelTopicObservation,
)
from omnimarket.nodes.node_broker_topic_removal_effect.adapters.declared_topics import (
    InMemoryDeclaredTopics,
)
from omnimarket.nodes.node_broker_topic_removal_effect.adapters.topic_inventory import (
    InMemoryTopicInventory,
)
from omnimarket.nodes.node_broker_topic_removal_effect.adapters.topic_remover import (
    InMemoryTopicRemover,
)
from omnimarket.nodes.node_broker_topic_removal_effect.handlers.handler_broker_topic_removal import (
    HandlerBrokerTopicRemoval,
)
from omnimarket.nodes.node_broker_topic_removal_effect.overlay import (
    ModelTopicRemovalOverlay,
)
from omnimarket.nodes.node_broker_topic_removal_effect.protocols import (
    DeclaredTopicsError,
    ProtocolDeclaredTopics,
)

pytestmark = pytest.mark.unit

NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_broker_topic_removal_effect"
)
EXAMPLE = NODE / "overlay.example.yaml"


def overlay() -> ModelTopicRemovalOverlay:
    """The node's example overlay: dry run by default, declared topics not removable."""
    return ModelTopicRemovalOverlay.model_validate(
        yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    )


#: The six topics of the default world: two produced, two never produced (one declared, one
#: undeclared), one never produced that a consumer group still reads, and one broker-internal.
TOPICS = {
    "a": "fixture-topic-a-produced",
    "b": "fixture-topic-b-declared-produced",
    "c": "fixture-topic-c-declared-never",
    "d": "fixture-topic-d-undeclared-never",
    "e": "fixture-topic-e-never-with-group",
    "internal": "_internal.topic",
}

OBSERVATIONS = (
    ModelTopicObservation(
        name=TOPICS["a"],
        partitions=1,
        high_watermark_total=100,
        consumer_groups=("g",),
        produced_bytes=100,
    ),
    ModelTopicObservation(
        name=TOPICS["b"],
        partitions=1,
        high_watermark_total=5,
        consumer_groups=(),
        produced_bytes=5,
    ),
    ModelTopicObservation(
        name=TOPICS["c"],
        partitions=1,
        high_watermark_total=0,
        consumer_groups=(),
        produced_bytes=0,
    ),
    ModelTopicObservation(
        name=TOPICS["d"],
        partitions=3,
        high_watermark_total=0,
        consumer_groups=(),
        produced_bytes=0,
    ),
    ModelTopicObservation(
        name=TOPICS["e"],
        partitions=1,
        high_watermark_total=0,
        consumer_groups=("g2",),
        produced_bytes=0,
    ),
    ModelTopicObservation(
        name=TOPICS["internal"],
        partitions=1,
        high_watermark_total=0,
        consumer_groups=(),
        produced_bytes=0,
    ),
)

#: Topic -> the contracts that declare it, whatever the role.
DECLARED: Mapping[str, tuple[str, ...]] = {
    TOPICS["b"]: ("c1",),
    TOPICS["c"]: ("c2",),
}
#: Topic -> the contracts that declare it as published.
PRODUCERS: Mapping[str, tuple[str, ...]] = {
    TOPICS["b"]: ("c1",),
    TOPICS["c"]: ("c2",),
}


class UnreadableDeclaredTopics:
    """The contracts' topics could not be read; the handler must refuse it by the typed reason."""

    def declared(self) -> dict[str, tuple[str, ...]]:
        raise DeclaredTopicsError("registry-root-variable-unset")

    def producers(self) -> dict[str, tuple[str, ...]]:
        raise DeclaredTopicsError("registry-root-variable-unset")


class World:
    """The broker's topics, the contracts' declarations and a remover; healthy until a test breaks one by name."""

    def __init__(self) -> None:
        self.observations: tuple[ModelTopicObservation, ...] | None = OBSERVATIONS
        self.declared: Mapping[str, tuple[str, ...]] = DECLARED
        self.producers: Mapping[str, tuple[str, ...]] = PRODUCERS
        self.remover = InMemoryTopicRemover()
        self.overlay = overlay()
        self.inventory = InMemoryTopicInventory(self.observations)
        self.declared_topics: ProtocolDeclaredTopics = InMemoryDeclaredTopics(
            self.declared, self.producers
        )

    def set_observations(
        self, observations: tuple[ModelTopicObservation, ...] | None
    ) -> None:
        self.observations = observations
        self.inventory = InMemoryTopicInventory(observations)

    def break_inventory_unreadable(self) -> None:
        """The broker's offsets could not be read; every run refuses it typed."""
        self.set_observations(None)

    def break_declared_unreadable(self) -> None:
        self.declared_topics = UnreadableDeclaredTopics()

    def break_remover_fails(self, reason: str) -> None:
        self.remover = InMemoryTopicRemover(fail_with=reason)

    def break_make_declared_removable(self) -> None:
        self.overlay = self.overlay.model_copy(update={"remove_declared_topics": True})

    def enable_execute(self) -> None:
        self.overlay = self.overlay.model_copy(update={"execute_enabled": True})

    def tiny_cap(self) -> None:
        self.overlay = self.overlay.model_copy(update={"max_topics_per_run": 1})

    def handler(self, remover: bool = False) -> HandlerBrokerTopicRemoval:
        return HandlerBrokerTopicRemoval(
            overlay=self.overlay,
            inventory=self.inventory,
            declared=self.declared_topics,
            remover=self.remover if remover else None,
        )


__all__ = [
    "DECLARED",
    "OBSERVATIONS",
    "PRODUCERS",
    "TOPICS",
    "UnreadableDeclaredTopics",
    "World",
    "overlay",
]
