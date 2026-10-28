# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Stub bindings of the bus protocol: the reference, and one mutation per defect of the 0d.4 matrix.

The reference is core's in-memory event bus, the binding the container registers by default.
Each mutation wraps it and breaks one rule the bus conformance suite checks.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.event_bus.model_event_headers import ModelEventHeaders
from omnibase_core.models.event_bus.model_event_message import ModelEventMessage

type MessageCallback = Callable[[ModelEventMessage], Awaitable[None]]


def build_reference_bus() -> EventBusInmemory:
    """The reference bus binding."""
    return EventBusInmemory(environment="stub", group="stub-reference")


class StubBusMutation:
    """Base of a mutation: delegates every call to the reference bus."""

    def __init__(self) -> None:
        self._inner = build_reference_bus()

    async def start(self) -> None:
        await self._inner.start()

    async def close(self) -> None:
        await self._inner.close()

    async def subscribe(
        self, topic: str, *, on_message: MessageCallback, group_id: str
    ) -> Callable[[], Awaitable[None]]:
        return await self._inner.subscribe(
            topic, on_message=on_message, group_id=group_id
        )

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: ModelEventHeaders | None = None,
    ) -> None:
        await self._inner.publish(topic, key, value, headers)


class StubBusOutOfOrder(StubBusMutation):
    """Delivers each pair of messages swapped: holds one back and releases it after the next."""

    def __init__(self) -> None:
        super().__init__()
        self._held: tuple[str, bytes | None, bytes, ModelEventHeaders | None] | None = (
            None
        )

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: ModelEventHeaders | None = None,
    ) -> None:
        if self._held is None:
            self._held = (topic, key, value, headers)
            return
        await self._inner.publish(topic, key, value, headers)
        held, self._held = self._held, None
        await self._inner.publish(*held)

    async def close(self) -> None:
        if self._held is not None:
            held, self._held = self._held, None
            await self._inner.publish(*held)
        await super().close()


class StubBusDuplicate(StubBusMutation):
    """Delivers every message twice, with no redelivery marker."""

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: ModelEventHeaders | None = None,
    ) -> None:
        await self._inner.publish(topic, key, value, headers)
        await self._inner.publish(topic, key, value, headers)


class StubBusDropsLast(StubBusMutation):
    """Drops every third message: the stub whose run loses a row."""

    def __init__(self) -> None:
        super().__init__()
        self._seen = 0

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: ModelEventHeaders | None = None,
    ) -> None:
        self._seen += 1
        if self._seen % 3 == 0:
            return
        await self._inner.publish(topic, key, value, headers)
