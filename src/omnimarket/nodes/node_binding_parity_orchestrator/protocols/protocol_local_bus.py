# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The slice of the event bus protocol the in-process entry, the stubs and the BUS suite use."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from omnibase_core.models.event_bus.model_event_headers import ModelEventHeaders
from omnibase_core.models.event_bus.model_event_message import ModelEventMessage


class ProtocolLocalBus(Protocol):
    """The slice of the event bus protocol the in-process entry uses."""

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: ModelEventHeaders | None = None,
    ) -> None: ...

    async def subscribe(
        self,
        topic: str,
        *,
        on_message: Callable[[ModelEventMessage], Awaitable[None]],
        group_id: str,
    ) -> Callable[[], Awaitable[None]]: ...
