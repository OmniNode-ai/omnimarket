# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_broker_topic_removal_effect: the broker's topics, the contracts' topics, the removal."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from omnimarket.events.topic_removal import (
    ModelTopicObservation,
)


class TopicInventoryError(RuntimeError):
    """The broker's topics could not be read; the message is a typed reason."""


class DeclaredTopicsError(RuntimeError):
    """The contracts' topics could not be read; the message is a typed reason."""


class TopicRemovalError(RuntimeError):
    """The broker refused or failed a delete; the message is a typed reason."""


class ProtocolTopicInventory(Protocol):
    def read(self) -> Sequence[ModelTopicObservation]:
        """Every user topic on the broker; raises ``TopicInventoryError`` with a typed reason."""
        ...


class ProtocolDeclaredTopics(Protocol):
    def declared(self) -> Mapping[str, tuple[str, ...]]:
        """Topic -> sorted names of the contracts declaring it, whatever the role; raises ``DeclaredTopicsError``."""
        ...

    def producers(self) -> Mapping[str, tuple[str, ...]]:
        """Topic -> sorted names of the contracts declaring it as published; raises ``DeclaredTopicsError``."""
        ...


class ProtocolTopicRemover(Protocol):
    def remove(self, names: Sequence[str]) -> tuple[str, ...]:
        """Delete the named topics and return the ones the broker confirmed gone; raises ``TopicRemovalError``."""
        ...


__all__ = [
    "DeclaredTopicsError",
    "ProtocolDeclaredTopics",
    "ProtocolTopicInventory",
    "ProtocolTopicRemover",
    "TopicInventoryError",
    "TopicRemovalError",
]
