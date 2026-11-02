# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bus the decision travels over: the in-memory binding or the Redpanda binding."""

from collections.abc import Awaitable, Callable
from typing import Protocol

from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage


class ProtocolLaunchValidityBus(Protocol):
    async def publish(self, topic: str, key: bytes | None, value: bytes) -> object: ...

    async def subscribe_ungrouped(
        self,
        topic: str,
        on_message: Callable[[ModelEventMessage], Awaitable[None]],
    ) -> Callable[[], Awaitable[None]]: ...
