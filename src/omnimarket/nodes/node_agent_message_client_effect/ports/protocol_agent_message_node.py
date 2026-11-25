# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam to the agent message node that owns the topic."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_agent_message_client_effect.models.model_agent_message import (
    ModelAgentMessage,
    ModelAgentMessageQuery,
    ModelAgentMessageReadReport,
)


class ProtocolAgentMessageNode(Protocol):
    """Publishes a message to, and reads messages from, the agent message topic."""

    def send(self, message: ModelAgentMessage) -> str | None:
        """Publish; the node's event id when it names one, which becomes the message id."""
        ...

    def read(self, query: ModelAgentMessageQuery) -> ModelAgentMessageReadReport: ...
