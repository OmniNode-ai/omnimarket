# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the reader gets the publications on a snapshot topic: held in memory by default, the broker as the real backend."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Protocol, cast

OpenBus = Callable[[], Any]


class ProtocolSnapshotSource(Protocol):
    """Every publication a topic holds within one read, oldest first. A broker that cannot be reached raises."""

    async def publications(self, topic: str) -> Sequence[bytes]: ...


class InMemorySnapshotSource:
    """The default: the publications a test or a drill holds, by topic."""

    def __init__(self, held: Mapping[str, Sequence[bytes]] | None = None) -> None:
        self._held = {topic: list(values) for topic, values in (held or {}).items()}

    async def publications(self, topic: str) -> Sequence[bytes]:
        return tuple(self._held.get(topic, ()))


class BusSnapshotSource:
    """Listens on a topic for one window from the earliest retained offset, under a fresh consumer group."""

    def __init__(
        self, *, open_bus: OpenBus, group_prefix: str, window_s: float
    ) -> None:
        self._open_bus = open_bus
        self._group_prefix = group_prefix
        self._window_s = window_s

    async def publications(self, topic: str) -> Sequence[bytes]:
        received: list[bytes] = []

        async def on_message(message: Any) -> None:
            received.append(bytes(message.value))

        async with self._open_bus() as bus:
            subscribe = cast("Callable[..., Awaitable[Any]]", bus.subscribe)
            # A broker replays from the earliest retained offset; the in-memory bus takes no such policy.
            extra = (
                {"auto_offset_reset": "earliest"}
                if "auto_offset_reset" in inspect.signature(bus.subscribe).parameters
                else {}
            )
            stop = await subscribe(
                topic,
                on_message=on_message,
                group_id=f"{self._group_prefix}.{uuid.uuid4().hex}",
                **extra,
            )
            try:
                await asyncio.sleep(self._window_s)
            finally:
                await stop()
        return tuple(received)
