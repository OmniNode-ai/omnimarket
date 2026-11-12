# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_broker_topic_delete_effect: the broker's live topic evidence, its deletes, and the contracts' topics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from omnimarket.nodes.node_broker_topic_delete_effect.models.model_topic_delete import (
    ModelLiveTopicEvidence,
)
from omnimarket.nodes.node_broker_topic_removal_effect.protocols import (
    DeclaredTopicsError,
    ProtocolDeclaredTopics,
)


class BrokerAdminError(RuntimeError):
    """The broker's evidence could not be read or a delete could not run; the message is a typed reason."""


class ProtocolBrokerTopicAdmin(Protocol):
    def read_evidence(
        self, names: Sequence[str]
    ) -> Mapping[str, ModelLiveTopicEvidence]:
        """Live evidence per named topic; raises ``BrokerAdminError`` with a typed reason. A name
        the broker does not hold maps to evidence with ``exists`` false."""
        ...

    def delete(self, names: Sequence[str]) -> Mapping[str, str]:
        """Delete the named topics; ``""`` per name the broker confirmed, else its error name;
        raises ``BrokerAdminError``."""
        ...


__all__ = [
    "BrokerAdminError",
    "DeclaredTopicsError",
    "ProtocolBrokerTopicAdmin",
    "ProtocolDeclaredTopics",
]
