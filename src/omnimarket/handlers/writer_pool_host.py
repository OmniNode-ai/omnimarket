# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One kept asyncpg pool for a projection writer (OMN-23257).

The runtime calls a writer's synchronous ``handle`` from short-lived worker
threads, up to the contract's ``consume_concurrency`` at once, and an asyncpg
pool belongs to the loop that opened it. The writers used to open a pool on a
fresh loop for every message, about seven connects a second on the broker host.
This host keeps one loop thread and one pool for the writer's life. Messages
run on that loop, each holding one connection for its statements, and at most
``max_size`` at a time.

Failure is typed and never opens a second pool. A closed host raises
:class:`WriterPoolClosedError`; a host whose ``max_size`` messages are all in
flight for ``acquire_timeout_seconds`` raises :class:`WriterPoolExhaustedError`.
Both leave the message uncommitted for the runtime to redeliver.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import logging
import threading
from collections.abc import Awaitable, Callable
from functools import cache
from typing import Any, TypeVar

from omnimarket.handlers.handler_packaged_contract_loader import (
    load_packaged_contract_section,
)
from omnimarket.models.model_writer_pool_settings import ModelWriterPoolSettings

logger = logging.getLogger(__name__)

WRITER_POOL_SECTION = "writer_pool"
SHUTDOWN_TIMEOUT_S = 30.0

_T = TypeVar("_T")


class WriterPoolError(RuntimeError):
    """A writer could not take its kept pool for a message."""


class WriterPoolClosedError(WriterPoolError):
    """The writer's pool was closed; it is never reopened for a later message."""


class WriterPoolExhaustedError(WriterPoolError):
    """Every connection stayed busy for the whole acquire timeout."""


@cache
def load_writer_pool_settings(node_name: str) -> ModelWriterPoolSettings:
    """The pool ``node_name`` declares; raises when its contract declares none."""
    return ModelWriterPoolSettings.model_validate(
        load_packaged_contract_section(node_name, WRITER_POOL_SECTION)
    )


class WriterPoolHost:
    """One loop thread and one pool; ``run`` puts a message's work on them."""

    def __init__(
        self,
        name: str,
        adapter_factory: Callable[[], Any],
        settings: ModelWriterPoolSettings,
    ) -> None:
        self._name = name
        self._adapter_factory = adapter_factory
        self._settings = settings
        self._lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._adapter: Any = None
        self._admission: asyncio.Semaphore | None = None
        self._closed = False

    @property
    def settings(self) -> ModelWriterPoolSettings:
        return self._settings

    @property
    def is_open(self) -> bool:
        return self._loop is not None and not self._closed

    def start(self) -> None:
        """Open the pool once; a later call finds it open, a closed host refuses."""
        with self._lock:
            if self._closed:
                raise WriterPoolClosedError(f"{self._name}: pool is closed")
            if self._loop is not None:
                return
            loop = asyncio.new_event_loop()
            started = threading.Event()

            def run_loop() -> None:
                asyncio.set_event_loop(loop)
                started.set()
                loop.run_forever()

            thread = threading.Thread(
                target=run_loop, name=f"{self._name}-pool-loop", daemon=True
            )
            thread.start()
            started.wait()
            adapter = self._adapter_factory()
            try:
                asyncio.run_coroutine_threadsafe(self._open(adapter), loop).result()
            except BaseException:
                self._stop_loop(loop, thread)
                raise
            self._loop, self._thread, self._adapter = loop, thread, adapter
            atexit.register(self.close)

    async def _open(self, adapter: Any) -> None:
        await adapter.connect()
        self._admission = asyncio.Semaphore(self._settings.max_size)

    def run(self, work: Callable[[Any], Awaitable[_T]]) -> _T:
        """Run one message's work on the kept pool and return its result."""
        self.start()
        loop = self._loop
        if loop is None:
            raise WriterPoolClosedError(f"{self._name}: pool is closed")
        if threading.current_thread() is self._thread:
            raise RuntimeError(f"{self._name}: run() called from the pool's own loop")
        try:
            future = asyncio.run_coroutine_threadsafe(self._guarded(work), loop)
        except RuntimeError as exc:
            raise WriterPoolClosedError(f"{self._name}: pool is closed") from exc
        return future.result()

    async def _guarded(self, work: Callable[[Any], Awaitable[_T]]) -> _T:
        admission = self._admission
        if self._closed or admission is None:
            raise WriterPoolClosedError(f"{self._name}: pool is closed")
        try:
            await asyncio.wait_for(
                admission.acquire(), self._settings.acquire_timeout_seconds
            )
        except TimeoutError as exc:
            raise WriterPoolExhaustedError(
                f"{self._name}: all {self._settings.max_size} connections busy for "
                f"{self._settings.acquire_timeout_seconds}s"
            ) from exc
        try:
            if self._closed:
                raise WriterPoolClosedError(f"{self._name}: pool is closed")
            return await work(self._adapter)
        finally:
            admission.release()

    def close(self, timeout_s: float = SHUTDOWN_TIMEOUT_S) -> None:
        """Let messages in flight finish, close the pool, stop the loop. Safe twice."""
        with self._lock:
            self._closed = True
            loop, thread, adapter = self._loop, self._thread, self._adapter
            admission = self._admission
            self._loop = self._thread = self._adapter = self._admission = None
        if loop is None or thread is None:
            return
        with contextlib.suppress(ValueError):
            atexit.unregister(self.close)

        async def release() -> None:
            if admission is not None:
                for _ in range(self._settings.max_size):
                    await admission.acquire()
            await adapter.close()

        try:
            asyncio.run_coroutine_threadsafe(release(), loop).result(timeout_s)
        except Exception:
            logger.exception("%s could not close its pool", self._name)
        finally:
            self._stop_loop(loop, thread, timeout_s)

    @staticmethod
    def _stop_loop(
        loop: asyncio.AbstractEventLoop,
        thread: threading.Thread,
        timeout_s: float = SHUTDOWN_TIMEOUT_S,
    ) -> None:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout_s)
        if not thread.is_alive():
            loop.close()


def build_pool_adapter(db: Any, settings: ModelWriterPoolSettings) -> Any:
    """The adapter a writer keeps: the runtime-bound DSN, sized by the contract.

    A test double is not an ``AsyncpgAdapter`` and is used as given.
    """
    # Lazy import, as in BaseProjectionRunner (OMN-15800 AC6).
    from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter

    if isinstance(db, AsyncpgAdapter):
        return AsyncpgAdapter(
            dsn=db.dsn,
            min_size=settings.min_size,
            max_size=settings.max_size,
            command_timeout=settings.command_timeout_seconds,
        )
    return db


__all__ = [
    "WRITER_POOL_SECTION",
    "WriterPoolClosedError",
    "WriterPoolError",
    "WriterPoolExhaustedError",
    "WriterPoolHost",
    "build_pool_adapter",
    "load_writer_pool_settings",
]
