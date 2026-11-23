# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Effect boundaries of the agent message node."""

from collections.abc import Awaitable, Callable
from typing import Protocol

from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

from omnimarket.nodes.node_event_emit_effect.models.model_emit_request import (
    ModelEmitRequest,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_result import (
    ModelEmitResult,
)


class ProtocolAgentMessageEmitter(Protocol):
    def handle(self, request: ModelEmitRequest) -> ModelEmitResult: ...


class ProtocolAgentMessageBus(Protocol):
    async def subscribe(
        self,
        topic: str,
        *,
        on_message: Callable[[ModelEventMessage], Awaitable[None]],
        group_id: str,
        auto_offset_reset: str,
    ) -> Callable[[], Awaitable[None]]: ...


__all__: list[str] = ["ProtocolAgentMessageBus", "ProtocolAgentMessageEmitter"]
