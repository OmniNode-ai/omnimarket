# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dispatch loop and the pool connected on it, held for the writer (OMN-21536).

The consumer-flow projection writer is dispatched in process, once per consumed
message, from worker threads. Every loop-bound resource it opens (the asyncpg pool
and the snapshot producer) must live on one long-lived event loop on a daemon
thread, to which each message is handed (OMN-17427, C28). That loop, its thread
and the pool connected on it are state, so they live here, in a collaborator the
writer is given, and not on the writer: no public writer method assigns to the
writer.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter

__all__: list[str] = ["DispatchSession"]


class DispatchSession:
    """One long-lived dispatch loop and the pool connected on it."""

    def __init__(self, thread_name: str) -> None:
        self._thread_name = thread_name
        self._guard = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._connect_lock: asyncio.Lock | None = None
        # The adapter whose pool this session opened, compared by identity so a
        # replaced adapter (the runtime's bind, a test double) is connected.
        self._connected_db: AsyncpgAdapter | None = None

    @property
    def thread(self) -> threading.Thread | None:
        """The dispatch loop's thread, or None before the first message."""
        return self._thread

    def run[T](self, coroutine: Coroutine[Any, Any, T]) -> T:
        """Run ``coroutine`` on the long-lived dispatch loop and wait for it."""
        with self._guard:
            loop = self._loop
            if loop is None or loop.is_closed():
                loop = asyncio.new_event_loop()
                thread = threading.Thread(
                    target=loop.run_forever,
                    name=self._thread_name,
                    daemon=True,
                )
                thread.start()
                self._loop = loop
                self._thread = thread
                self._connect_lock = None
                self._connected_db = None
        return asyncio.run_coroutine_threadsafe(coroutine, loop).result()

    async def ensure_connected(self, db: AsyncpgAdapter) -> None:
        """Open ``db``'s pool once on the running loop, and again only if ``db`` changed."""
        if self._connected_db is db:
            return
        if self._connect_lock is None:
            self._connect_lock = asyncio.Lock()
        async with self._connect_lock:
            if self._connected_db is db:
                return
            await db.connect()
            self._connected_db = db

    async def release_connection(self) -> None:
        """Close the pool this session connected, if any."""
        if self._connected_db is not None:
            connected = self._connected_db
            self._connected_db = None
            await connected.close()

    def stop(self, release: Callable[[], Coroutine[Any, Any, None]]) -> None:
        """Run ``release`` on the dispatch loop, then stop and close the loop."""
        with self._guard:
            loop = self._loop
            thread = self._thread
            self._loop = None
            self._thread = None
        if loop is None or loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(release(), loop).result()
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join()
        loop.close()
