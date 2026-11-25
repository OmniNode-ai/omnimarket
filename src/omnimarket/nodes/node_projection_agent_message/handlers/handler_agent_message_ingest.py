# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read a batch from the injected reader, project each record and put it in the injected store."""

from __future__ import annotations

from omnimarket.nodes.node_projection_agent_message.handlers.handler_projection_agent_message import (
    HandlerProjectionAgentMessage,
)
from omnimarket.nodes.node_projection_agent_message.models import (
    EnumAgentMessageRowStatus,
    ModelAgentMessageIngestRequest,
    ModelAgentMessageIngestResult,
)
from omnimarket.nodes.node_projection_agent_message.ports.protocol_agent_message_reader import (
    ProtocolAgentMessageReader,
)
from omnimarket.nodes.node_projection_agent_message.ports.protocol_agent_message_store import (
    ProtocolAgentMessageStore,
)


class HandlerAgentMessageIngest:
    """One ingest pass; every record read is counted as projected, refused or a duplicate."""

    def __init__(
        self,
        *,
        reader: ProtocolAgentMessageReader,
        store: ProtocolAgentMessageStore,
        projection: HandlerProjectionAgentMessage | None = None,
    ) -> None:
        self._reader = reader
        self._store = store
        self._projection = projection or HandlerProjectionAgentMessage()

    async def handle(
        self, request: ModelAgentMessageIngestRequest
    ) -> ModelAgentMessageIngestResult:
        deliveries = await self._reader.read_batch(request.max_count)
        projected = refused = duplicates = 0
        for delivery in deliveries:
            row = self._projection.handle(delivery)
            if not await self._store.put(row):
                duplicates += 1
            elif row.status is EnumAgentMessageRowStatus.OK:
                projected += 1
            else:
                refused += 1
        return ModelAgentMessageIngestResult(
            read=len(deliveries),
            projected=projected,
            refused=refused,
            duplicates=duplicates,
        )


__all__ = ["HandlerAgentMessageIngest"]
