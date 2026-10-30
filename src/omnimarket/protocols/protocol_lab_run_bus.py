# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Publish and grouped subscription ports shared by lab bus bindings."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

MessageCallback = Callable[[Any], Awaitable[None]]


class ProtocolLabRunBus(Protocol):
    """The two calls both sides make on the bus (in-memory or Kafka)."""

    async def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: Any = None
    ) -> object: ...

    async def subscribe(
        self,
        topic: str,
        node_identity: Any = None,
        on_message: MessageCallback | None = None,
        *,
        group_id: str | None = None,
    ) -> Callable[[], Awaitable[None]]: ...
