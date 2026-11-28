# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The drain state on the bus (OMN-24489): the mark rides the capacity advertisement every pool host already publishes.

A drain command goes to the bus (``ModelHostDrainCommand`` on the drain node's command topic); the addressed host's serve
process holds it and advertises it on every beat as the ``draining-until:`` pseudo-tool, beside the lanes it runs
(``lane-running:``) and its running units. Every launcher, the lab fill and this verb read the same advertisement, so no
machine keeps a drain state of its own and no second reader counts what the host already says.

* :class:`SyncBus` owns one bus connection on a loop of its own, so the synchronous drain verb can publish and listen.
* :class:`BusAdvertisementSource` listens for one window (the capacity reader of the lab-work caller, reused).
* :class:`BusDrainCommandPublisher` publishes one drain command.
* :class:`AdvertisedLaneSource` reads a host's lanes, units and hold from the advertisements.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from typing import Any, TypeVar

from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.delegated_test_loop.lab_run_bus import (
    ProtocolLabRunBus,
    event_type_for,
)
from omnimarket.lab_work.bus import LabWorkCaller
from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.models.model_host_drain_command import (
    ModelHostDrainCommand,
    load_host_drain_topic,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols.local_host_reader import (
    running_lanes_from_tools,
)

T = TypeVar("T")

OpenBus = Callable[[], contextlib.AbstractAsyncContextManager[ProtocolLabRunBus]]


class SyncBus:
    """One bus connection, owned by a loop on a thread of its own: a synchronous caller runs coroutines on it.

    ``SyncBus(open_bus)`` starts the thread and the connection; ``SyncBus.over(loop, bus)`` rides a loop that is already
    running (a test that hosts the bus itself). ``close`` ends what this object started and nothing else.
    """

    def __init__(
        self,
        open_bus: OpenBus | None = None,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
        bus: ProtocolLabRunBus | None = None,
        timeout_seconds: float,
    ) -> None:
        self._timeout = timeout_seconds
        self._thread: threading.Thread | None = None
        self._stop: asyncio.Event | None = None
        if open_bus is None:
            if loop is None or bus is None:
                raise ValueError("SyncBus needs open_bus, or a loop and a bus")
            self._loop, self.bus = loop, bus
            return
        self._loop = asyncio.new_event_loop()
        ready: Future[ProtocolLabRunBus] = Future()

        async def serve() -> None:
            self._stop = asyncio.Event()
            try:
                async with open_bus() as opened:
                    ready.set_result(opened)
                    await self._stop.wait()
            except BaseException as exc:
                if not ready.done():
                    ready.set_exception(exc)
                else:
                    raise

        self._thread = threading.Thread(
            target=self._loop.run_until_complete,
            args=(serve(),),
            name="drain-bus",
            daemon=True,
        )
        self._thread.start()
        self.bus = ready.result(timeout=timeout_seconds)

    @classmethod
    def over(
        cls,
        loop: asyncio.AbstractEventLoop,
        bus: ProtocolLabRunBus,
        *,
        timeout_seconds: float,
    ) -> SyncBus:
        return cls(loop=loop, bus=bus, timeout_seconds=timeout_seconds)

    def run(self, coroutine: Awaitable[T]) -> T:
        """Run a coroutine on the bus loop and wait for it; a bus that does not answer in time raises OSError."""
        future = asyncio.run_coroutine_threadsafe(_as_coroutine(coroutine), self._loop)
        try:
            return future.result(timeout=self._timeout)
        except TimeoutError as exc:
            future.cancel()
            raise OSError(f"the bus did not answer within {self._timeout}s") from exc

    def close(self) -> None:
        if self._thread is None or self._stop is None:
            return
        self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=self._timeout)


async def _as_coroutine[T](awaitable: Awaitable[T]) -> T:
    return await awaitable


class BusAdvertisementSource:
    """Listens on the capacity topic for one window and keeps what it heard for ``cache_seconds``.

    The window is the lab-work caller's own read (``LabWorkCaller.collect``): no consumer group, the log end, every live
    host advertising once per cadence. The cache lets one status read the window once; a write invalidates it.
    """

    def __init__(
        self,
        sync_bus: SyncBus,
        *,
        window_seconds: float,
        cache_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sync = sync_bus
        self._window = window_seconds
        self._cache_seconds = cache_seconds
        self._clock = clock
        self._heard: tuple[ModelHostCapacityAdvertisement, ...] = ()
        self._heard_at: float | None = None

    def invalidate(self) -> None:
        self._heard_at = None

    def advertisements(self) -> tuple[ModelHostCapacityAdvertisement, ...]:
        now = self._clock()
        if self._heard_at is not None and now - self._heard_at < self._cache_seconds:
            return self._heard
        self._heard = tuple(
            sorted(self._sync.run(self._listen()), key=lambda ad: ad.advertised_at)
        )
        self._heard_at = self._clock()
        return self._heard

    async def _listen(self) -> list[ModelHostCapacityAdvertisement]:
        caller = LabWorkCaller(self._sync.bus)
        await caller.start()
        try:
            return await caller.collect(self._window)
        finally:
            await caller.stop()


class BusDrainCommandPublisher:
    """Publishes a drain command on the drain node's command topic, keyed by the host, and forgets the advertisements read."""

    def __init__(
        self, sync_bus: SyncBus, source: BusAdvertisementSource | None = None
    ) -> None:
        self._sync = sync_bus
        self._source = source
        self._topic = load_host_drain_topic()

    def publish(self, command: ModelHostDrainCommand) -> None:
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=command.model_dump(mode="json"),
            correlation_id=uuid.uuid4(),
            event_type=event_type_for(self._topic),
        )
        value = json.dumps(envelope.model_dump(mode="json")).encode("utf-8")
        self._sync.run(
            _publish(self._sync.bus, self._topic, command.host_name.encode(), value)
        )
        if self._source is not None:
            self._source.invalidate()


async def _publish(
    bus: ProtocolLabRunBus, topic: str, key: bytes, value: bytes
) -> None:
    await bus.publish(topic, key, value)


def host_advertisements(
    source: Any, host_name: str
) -> tuple[ModelHostCapacityAdvertisement, ...]:
    """One host's advertisements of the window, oldest first."""
    return tuple(ad for ad in source.advertisements() if ad.host_name == host_name)


class AdvertisedLaneSource:
    """What a host runs, read from the host's own advertisements: its units and its lanes.

    The window must hold at least two advertisements of the host (two readings) and every one must carry the lane read:
    the count is the largest ``running_units + lanes`` any advertisement of the window shows, so a lane that appeared
    between two beats shows. A host that advertised nothing, advertised once, or advertised without reading its lanes
    raises ValueError: an unreadable count is never a measured zero.
    """

    def __init__(self, source: Any, *, min_advertisements: int = 2) -> None:
        self._source = source
        self._min = min_advertisements

    def _ads(self, host_name: str) -> tuple[ModelHostCapacityAdvertisement, ...]:
        ads = host_advertisements(self._source, host_name)
        if not ads:
            raise ValueError(f"{host_name} advertised nothing in the window")
        return ads

    def running(self, host_name: str) -> int:
        ads = self._ads(host_name)
        if len(ads) < self._min:
            raise ValueError(
                f"{host_name} advertised {len(ads)} time(s) in the window: a reading needs {self._min}"
            )
        counts: list[int] = []
        for ad in ads:
            lanes = running_lanes_from_tools(ad.tools)
            if lanes is None:
                raise ValueError(
                    f"{host_name} advertised without reading its lanes (its serve predates the lane read, or the read failed)"
                )
            counts.append(ad.running_units + len(lanes))
        return max(counts)

    def names(self, host_name: str) -> tuple[str, ...]:
        found: set[str] = set()
        for ad in self._ads(host_name):
            found.update(running_lanes_from_tools(ad.tools) or ())
        return tuple(sorted(found))

    def held_reason(self, host_name: str) -> str:
        ad = self._ads(host_name)[-1]
        return ad.capacity_refusal


__all__ = [
    "AdvertisedLaneSource",
    "BusAdvertisementSource",
    "BusDrainCommandPublisher",
    "SyncBus",
    "host_advertisements",
]
