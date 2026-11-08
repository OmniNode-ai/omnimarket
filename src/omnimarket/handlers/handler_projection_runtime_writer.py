# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Dispatch existing projection SQL and snapshots without a second consume loop."""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import logging
import threading
from typing import Any, ClassVar

from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
    deterministic_correlation_id,
)

logger = logging.getLogger(__name__)

_SHUTDOWN_TIMEOUT_S = 30.0


class RuntimeProjectionWriter:
    """One runtime delivery, applied on pools this writer keeps for its process.

    Composition keeps the Kafka ``run()`` entrypoint out of handler routing.
    Each delivery uses the existing writer's ``project_event`` implementation,
    including its snapshot and DLQ paths. The existing per-table topology
    resolver opens and attests each workload binding; this matters for savings,
    whose tenant writes and registry reads cannot share one login role.

    OMN-23196. The runner and its pools live for the writer's process, on one
    dedicated event loop (an asyncpg pool belongs to the loop that opened it, and
    the runtime calls ``handle`` from short-lived worker threads). Opening and
    closing two pools for every delivery held each offset commit back by about
    0.17 s a delivery, which the consumer-group lag alarm read as growth. The
    commit is still per delivery: deliveries run one at a time, each statement in
    its own transaction on a pooled connection, so a failing delivery rolls back
    alone. :meth:`close` closes the pools; it runs at interpreter exit too.

    Profile names identify subscriptions, not processes. The composing runtime
    must activate the writer profiles and supply ONEX_DATABASE_TOPOLOGY_PROFILE
    and the topology's workload bindings, just as the dedicated writers did.
    """

    onex_runtime_inprocess_dispatch = True
    runner_type: ClassVar[type[BaseProjectionRunner]]

    def __init__(self) -> None:
        self._host_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._runner: BaseProjectionRunner | None = None
        self._delivery_lock: asyncio.Lock | None = None

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """Start the writer's own event loop thread on first use."""
        with self._host_lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                started = threading.Event()

                def run_loop() -> None:
                    asyncio.set_event_loop(loop)
                    started.set()
                    loop.run_forever()

                thread = threading.Thread(
                    target=run_loop,
                    name=f"{type(self).__name__}-loop",
                    daemon=True,
                )
                thread.start()
                started.wait()
                self._loop = loop
                self._thread = thread
                atexit.register(self.close)
            return self._loop

    async def _deliver(
        self, topic: str, payload: dict[str, Any], meta: MessageMeta
    ) -> bool:
        """Run one delivery on the writer's loop, one at a time."""
        if self._runner is None or self._delivery_lock is None:
            self._runner = self.runner_type()
            self._delivery_lock = asyncio.Lock()
        async with self._delivery_lock:
            return await self._runner.project_runtime_event(topic, payload, meta)

    def close(self, timeout_s: float = _SHUTDOWN_TIMEOUT_S) -> None:
        """Close the pools after the delivery in flight, then stop the loop.

        Safe to call twice; a later ``handle`` starts a fresh loop and runner.
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
            logger.exception("%s could not close its pools", type(self).__name__)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout_s)
            if not thread.is_alive():
                loop.close()

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        """Apply one delivery; never start a consumer. Pools outlive the call."""
        data = dict(request)
        # The runtime's synchronous adapter is not an event payload or the
        # asynchronous writer's pool. Per-binding connections below are
        # resolved and attested by the same topology as that injected adapter.
        data.pop("_db", None)
        topic = str(data.pop("_topic"))
        partition = int(data.pop("_partition"))
        offset = int(data.pop("_offset"))
        meta = MessageMeta(
            topic=topic,
            partition=partition,
            offset=offset,
            fallback_id=str(
                data.pop(
                    "_fallback_id",
                    deterministic_correlation_id(topic, partition, offset),
                )
            ),
        )
        loop = self._ensure_loop()
        projected = asyncio.run_coroutine_threadsafe(
            self._deliver(topic, self.event_payload(data), meta), loop
        ).result()
        return {"projected": projected}

    def event_payload(self, data: dict[str, Any]) -> dict[str, Any]:
        """Return the payload ``project_event`` receives, unchanged by default.

        A subclass narrows the payload here rather than overriding ``handle``,
        so every writer reads the source coordinates in one place.
        """
        return data
