# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host a projection runner and its pools on one event loop for a process (OMN-23196).

An asyncpg pool belongs to the loop that opened it, and the runtime calls a
writer's synchronous ``handle`` from short-lived worker threads. Opening and
closing two pools for every delivery held each offset commit back by about
0.17 s, which the consumer-group lag alarm read as growth. This host keeps one
loop thread, builds the runner on it once, and runs deliveries on it one at a
time. Each statement still commits in its own transaction on a pooled
connection, so a failing delivery rolls back alone.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import logging
import threading
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from omnimarket.models.model_message_meta import MessageMeta

if TYPE_CHECKING:
    from omnimarket.projection.runner import BaseProjectionRunner

logger = logging.getLogger(__name__)

SHUTDOWN_TIMEOUT_S = 30.0


def invalidates_runtime_pools(err: BaseException) -> bool:
    """Whether a delivery failure says the kept pools can no longer be trusted.

    A transport, driver-state or login failure means a pool may hold dead
    connections or credentials since rotated, so the next delivery opens and
    attests fresh ones. Every other failure (a bad payload, a constraint, a
    missing column) belongs to the delivery alone and leaves the pools open.
    """
    import asyncpg  # only the runtime process may load the driver (OMN-15800)

    return isinstance(
        err,
        (
            OSError,
            TimeoutError,
            asyncpg.InterfaceError,
            asyncpg.exceptions.PostgresConnectionError,
            asyncpg.exceptions.InvalidAuthorizationSpecificationError,
        ),
    )


class KeptPools:
    """The pool lifecycle of a runner that applies runtime deliveries.

    The runner hands over its own lifecycle calls, so this class never reaches
    into the runner. The first delivery opens the pools; later ones find them
    open. The producer is still stopped after every delivery. A
    connection-class failure closes the pools, so the next delivery
    re-resolves its credentials and attests them again, as every delivery did
    before OMN-23196.
    """

    def __init__(
        self,
        connect: Callable[[], Awaitable[None]],
        close: Callable[[], Awaitable[None]],
        stop_producer: Callable[[], Awaitable[None]],
    ) -> None:
        self._connect = connect
        self._close = close
        self._stop_producer = stop_producer
        self._open = False

    async def project(
        self,
        project_event: Callable[[str, dict[str, Any], MessageMeta], Awaitable[bool]],
        topic: str,
        data: dict[str, Any],
        meta: MessageMeta,
    ) -> bool:
        """Apply one delivery on the kept pools, opening them on the first."""
        if not self._open:
            await self._connect()
            self._open = True
        try:
            return await project_event(topic, data, meta)
        except BaseException as err:
            if invalidates_runtime_pools(err):
                await self.close()
            raise
        finally:
            await self._stop_producer()

    async def close(self) -> None:
        """Stop the producer and close every pool the deliveries opened."""
        try:
            await self._stop_producer()
        finally:
            self._open = False
            await self._close()


class RuntimeDeliveryHost:
    """One dedicated loop thread, one runner, deliveries one at a time."""

    def __init__(
        self,
        runner_factory: Callable[[], BaseProjectionRunner],
        name: str = "runtime-delivery",
    ) -> None:
        self._name = name
        self._runner_factory = runner_factory
        self._host_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._runner: BaseProjectionRunner | None = None
        self._delivery_lock: asyncio.Lock | None = None

    @property
    def thread(self) -> threading.Thread | None:
        """The loop thread, or None before the first delivery and after close."""
        return self._thread

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._host_lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                started = threading.Event()

                def run_loop() -> None:
                    asyncio.set_event_loop(loop)
                    started.set()
                    loop.run_forever()

                thread = threading.Thread(
                    target=run_loop, name=f"{self._name}-loop", daemon=True
                )
                thread.start()
                started.wait()
                self._loop, self._thread = loop, thread
                atexit.register(self.close)
            return self._loop

    async def _deliver(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        if self._runner is None or self._delivery_lock is None:
            self._runner = self._runner_factory()
            self._delivery_lock = asyncio.Lock()
        async with self._delivery_lock:
            return await self._runner.project_runtime_event(topic, data, meta)

    def deliver(self, topic: str, data: dict[str, Any], meta: MessageMeta) -> bool:
        """Apply one delivery on the host's loop and wait for its result."""
        return asyncio.run_coroutine_threadsafe(
            self._deliver(topic, data, meta), self._ensure_loop()
        ).result()

    def close(self, timeout_s: float = SHUTDOWN_TIMEOUT_S) -> None:
        """Close the pools after the delivery in flight, then stop the loop.

        Safe to call twice; a later ``deliver`` starts a fresh loop and runner.
        """
        with self._host_lock:
            loop, thread = self._loop, self._thread
            runner, delivery_lock = self._runner, self._delivery_lock
            self._loop = self._thread = self._runner = self._delivery_lock = None
        if loop is None or thread is None:
            return
        with contextlib.suppress(ValueError):
            atexit.unregister(self.close)

        async def release() -> None:
            if runner is None or delivery_lock is None:
                return
            async with delivery_lock:
                await runner.close_runtime_resources()

        try:
            asyncio.run_coroutine_threadsafe(release(), loop).result(timeout_s)
        except Exception:
            logger.exception("%s could not close its pools", self._name)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout_s)
            if not thread.is_alive():
                loop.close()
