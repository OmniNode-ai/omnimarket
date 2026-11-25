# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the ingest handler asks of a reader of the agent message topic."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_projection_agent_message.models import (
    ModelAgentMessageDelivery,
)


class ProtocolAgentMessageReader(Protocol):
    async def read_batch(self, max_count: int) -> list[ModelAgentMessageDelivery]:
        """At most ``max_count`` next records, oldest first; an empty list when the topic has no more."""
        ...
