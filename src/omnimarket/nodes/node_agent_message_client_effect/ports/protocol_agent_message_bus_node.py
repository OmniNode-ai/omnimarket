# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The agent message node as it is driven on an open bus: awaited, one event loop."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_agent_message_client_effect.models.model_agent_message import (
    ModelAgentMessage,
    ModelAgentMessageQuery,
    ModelAgentMessageReadReport,
)


class ProtocolAgentMessageBusNode(Protocol):
    async def send(self, message: ModelAgentMessage) -> str | None: ...

    async def read(
        self, query: ModelAgentMessageQuery
    ) -> ModelAgentMessageReadReport: ...
