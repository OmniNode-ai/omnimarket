# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An in-memory event bus transport for the emit effect's request/result seam (OMN-17427).

For a local runtime with no broker, ``InMemoryBusEmitter`` is the
``ProtocolEventEmitter`` that ``HandlerWorkLedgerEmit`` takes when the ledger
runs on the in-memory bus.
"""

from __future__ import annotations

import asyncio
import json
from typing import Protocol

from omnimarket.nodes.node_event_emit_effect.models.model_emit_request import (
    ModelEmitRequest,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_result import (
    ModelEmitResult,
)
from omnimarket.nodes.node_event_emit_effect.spool.topic_resolver import (
    resolve_event_type,
)


class ProtocolBusPublisher(Protocol):
    """The publish operation the in-memory emitter needs from its event bus."""

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> None: ...


class InMemoryBusEmitter:
    """Publishes an emit request's payload on the in-memory bus, not the Kafka spool."""

    def __init__(self, bus: ProtocolBusPublisher) -> None:
        self._bus = bus

    async def ahandle(self, request: ModelEmitRequest) -> ModelEmitResult:
        topics = (
            [request.topic]
            if request.topic is not None
            else [t.topic for t in resolve_event_type(request.event_type)]
        )
        value = json.dumps(request.payload).encode("utf-8")
        key = (request.partition_key or request.correlation_id or "").encode() or None
        for topic in topics:
            await self._bus.publish(topic, key, value)
        return ModelEmitResult(
            event_id=request.event_id, topics_published=topics, published=True
        )

    def handle(self, request: ModelEmitRequest) -> ModelEmitResult:
        """The synchronous ``ProtocolEventEmitter`` call; use ``ahandle`` inside a running loop."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.ahandle(request))
        raise RuntimeError(
            "InMemoryBusEmitter.handle called inside a running event loop; "
            "await ahandle instead"
        )


__all__ = ["InMemoryBusEmitter", "ProtocolBusPublisher"]
