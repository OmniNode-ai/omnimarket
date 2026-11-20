# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bus publisher of host-down readings: one publication per reading on the reading topic."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractAsyncContextManager
from typing import Any


class BusReadingPublisher:
    """Publishes each reading on the bus in a worker thread, like BusEmissionMirror.

    The publication runs under its own loop, so it is safe to call from a running event loop and from none.
    """

    def __init__(
        self,
        open_bus: Callable[[], AbstractAsyncContextManager[Any]],
        timeout_s: float,
    ) -> None:
        self._open_bus = open_bus
        self._timeout_s = timeout_s

    async def _publish(self, topic: str, key: str, payload: bytes) -> None:
        async with self._open_bus() as bus:
            await bus.publish(topic, key.encode("utf-8"), payload)

    def publish(self, topic: str, key: str, payload: bytes) -> None:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(
                asyncio.run,
                asyncio.wait_for(
                    self._publish(topic, key, payload),
                    timeout=self._timeout_s,
                ),
            ).result()
