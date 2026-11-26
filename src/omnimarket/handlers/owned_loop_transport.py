# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One bus transport on one background event loop, for sync callers (OMN-25912).

The sync snapshot publisher is called from worker threads that run no event
loop, while the Kafka producer inside ``KafkaTransport`` is bound to the loop
that started it. This holder owns one loop thread and one started transport for
its lifetime: ``run`` executes a coroutine on that loop, ``transport`` builds
and starts the transport on first use, ``drop`` discards it after a failed send
so the next ``transport`` call builds a new one, and ``close`` stops both.

It lives in ``handlers/`` rather than in ``projection/snapshot_publisher.py``
because that file is baselined by the canonical-file-shape ratchet and only
shrinks.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class OwnedLoopTransport:
    def __init__(self, *, bootstrap_servers: str) -> None:
        self._bootstrap_servers = bootstrap_servers
        self._guard = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        # Touched only on ``self._loop``.
        self._transport: Any = None

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._guard:
            if self._loop is None or self._loop.is_closed():
                loop = asyncio.new_event_loop()
                thread = threading.Thread(
                    target=loop.run_forever,
                    name="snapshot-delta-publisher",
                    daemon=True,
                )
                thread.start()
                self._loop, self._thread = loop, thread
            return self._loop

    def run(self, coro: Coroutine[Any, Any, _T]) -> _T:
        """Run ``coro`` on the owned loop and return its result."""
        return asyncio.run_coroutine_threadsafe(coro, self._ensure_loop()).result()

    async def transport(self, topic: str) -> Any:
        """Return the started transport, building it on first use; None if start fails."""
        if self._transport is not None:
            return self._transport
        # Lazy import (OMN-15800 AC6): the projection-api process imports names
        # from the projection package but must never load asyncpg, which
        # ``omnibase_infra``'s top-level __init__ chain pulls in transitively.
        from omnibase_infra.event_bus.kafka_transport import KafkaTransport

        # topics=() keeps this producer-only: start() then skips the consumer
        # leg entirely, so no consumer group is created and nothing joins a
        # rebalance just to send one record.
        transport = KafkaTransport.from_bootstrap(self._bootstrap_servers, topics=())
        try:
            await transport.start()
        except Exception as exc:
            logger.warning(
                "snapshot delta for %s not published: transport failed to start: %s",
                topic,
                exc,
            )
            return None
        self._transport = transport
        return transport

    async def drop(self) -> None:
        transport, self._transport = self._transport, None
        if transport is not None:
            try:
                await transport.close()
            except Exception as exc:
                logger.warning("snapshot delta transport close failed: %s", exc)

    def close(self) -> None:
        """Stop the transport and the loop; a later ``run`` starts anew."""
        with self._guard:
            loop, thread = self._loop, self._thread
            self._loop = self._thread = None
        if loop is None or thread is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self.drop(), loop).result()
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join()
            loop.close()
