# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two sinks of the pool's mark events: one held in memory, one published on the bus (OMN-19998).

The bus sink copies the shape of ``handlers.emission_mirror``: the publication runs in a worker
thread under its own event loop, so it is safe to call from a running event loop and from none, and
the bus is whatever the caller's opener hands back (``handlers.overlay_store.bus_opener`` opens the
lane's bus, exactly as the emission mirror opens its own). A refused publication raises
:class:`PoolMarkEventPublishError`, which names the topic and the type of the refusal — and no
value of the event, so an error line never echoes a mark back onto a log.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractAsyncContextManager
from typing import Any

from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool_mark_event import (
    ModelPoolMarkEvent,
    ModelPoolMarkEventsSettings,
)


class PoolMarkEventPublishError(Exception):
    """The bus refused one mark event: the topic it was refused on and the type of the refusal.

    No value of the event is carried. The message names the topic and the exception type only;
    the refused event stays chained as ``__cause__``.
    """

    def __init__(self, topic: str, cause: BaseException) -> None:
        self.topic = topic
        self.error_type = type(cause).__name__
        super().__init__(f"topic={topic} error={self.error_type}")


class InMemoryPoolMarkEventSink:
    """Holds each published event in order; the sink a test or an in-memory bus reads back."""

    def __init__(self) -> None:
        self.events: list[ModelPoolMarkEvent] = []

    def publish(self, event: ModelPoolMarkEvent) -> None:
        self.events.append(event)


class BusPoolMarkEventSink:
    """The real backend: one publication of the event's JSON on the settings' topic, keyed by ``host:label``.

    The settings' ``actor_env`` is the lane's concern — it names the environment variable the
    caller resolves ``who`` from before it builds an event; the sink needs only the topic.
    """

    def __init__(
        self,
        settings: ModelPoolMarkEventsSettings,
        open_bus: Callable[[], AbstractAsyncContextManager[Any]],
    ) -> None:
        self._settings = settings
        self._open_bus = open_bus

    async def _publish(self, event: ModelPoolMarkEvent) -> None:
        async with self._open_bus() as bus:
            await bus.publish(
                self._settings.topic,
                f"{event.host}:{event.label}".encode(),
                event.model_dump_json().encode("utf-8"),
            )

    def publish(self, event: ModelPoolMarkEvent) -> None:
        with ThreadPoolExecutor(max_workers=1) as pool:
            try:
                pool.submit(asyncio.run, self._publish(event)).result()
            except Exception as exc:
                raise PoolMarkEventPublishError(self._settings.topic, exc) from exc


__all__ = [
    "BusPoolMarkEventSink",
    "InMemoryPoolMarkEventSink",
    "PoolMarkEventPublishError",
]
