# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Effect boundaries of the agent message node."""

from collections.abc import Awaitable, Callable
from typing import Protocol

from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

from omnimarket.models.agent_message.model_agent_signing_key import (
    ModelAgentSigningKey,
)
from omnimarket.models.agent_message.model_registered_agent_sender import (
    ModelRegisteredAgentSender,
)
from omnimarket.nodes.node_event_emit_effect import ModelEmitRequest, ModelEmitResult


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


class ProtocolAgentSenderRegistry(Protocol):
    """The sender registry and the cutover flag, both read from the settings store (never from the message)."""

    async def lookup(self, name: str) -> ModelRegisteredAgentSender | None: ...

    async def accepts_legacy(self) -> bool: ...


class ProtocolAgentSigningKeys(Protocol):
    """The signing keys this host holds, by agent name, read from the settings store."""

    async def signing_key(self, name: str) -> ModelAgentSigningKey | None: ...


__all__: list[str] = [
    "ProtocolAgentMessageBus",
    "ProtocolAgentMessageEmitter",
    "ProtocolAgentSenderRegistry",
    "ProtocolAgentSigningKeys",
]
