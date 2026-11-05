# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Serialize each logical head across processes; retain acknowledged commands.

A crash before acknowledgement retries the same command id. Kafka delivery can
repeat after an ambiguous acknowledgement; the dispatch node's terminal log then
replays that id without another review. A journal is not an exactly-once broker.
"""

import asyncio
import fcntl
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path


class FileDispatchJournal:
    def __init__(self, root: Path, *, poll_seconds: float) -> None:
        self._root = root
        self._poll_seconds = poll_seconds

    @asynccontextmanager
    async def lock(self, command_id: str) -> AsyncIterator[None]:
        self._root.mkdir(parents=True, exist_ok=True)
        with (self._root / f"{command_id}.lock").open("a") as lock:
            # A nonblocking flock yields without holding up the runtime event loop.
            # The descriptor closes on cancellation and releases its process lock.
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(self._poll_seconds)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def sent(self, command_id: str) -> bool:
        return (self._root / command_id).is_file()

    def record(self, command_id: str) -> None:
        with (self._root / command_id).open("a") as marker:
            marker.flush()
            os.fsync(marker.fileno())
        descriptor = os.open(self._root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
