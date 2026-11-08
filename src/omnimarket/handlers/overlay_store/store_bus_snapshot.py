# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host-side store: the snapshot topic the projection publishes after it commits.

Nothing but a runtime connects to a database (operator ruling 2026-08-09), so a host reads the
projection from the bus. One instance listens for one window from the earliest retained offset under a
fresh consumer group, keeps the newest record of each overlay, and answers every read from that fold. A
window with no record of an overlay is the store holding none. A broker that cannot be reached raises.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any, cast

from omnibase_core.types import JsonType

OpenBus = Callable[[], Any]


class OverlayStoreReadOnlyError(RuntimeError):
    """A host asked to write the store: the loader publishes and the projection stores."""


class BusSnapshotOverlayStore:
    """Reads the snapshot topic; a host never writes the store."""

    def __init__(
        self,
        *,
        open_bus: OpenBus,
        topic: str,
        group_prefix: str,
        window_s: float,
    ) -> None:
        self._open_bus = open_bus
        self._topic = topic
        self._group_prefix = group_prefix
        self._window_s = window_s
        self._newest: dict[str, dict[str, JsonType]] | None = None
        self.malformed = 0

    async def write_version(self, record: Mapping[str, JsonType]) -> bool:
        raise OverlayStoreReadOnlyError(
            f"a host writes no version of {record.get('overlay_id')}: "
            "the loader publishes and the projection stores"
        )

    async def read_current(self, overlay_id: str) -> Mapping[str, JsonType] | None:
        if self._newest is None:
            self._newest = await self._listen()
        return self._newest.get(overlay_id)

    async def _listen(self) -> dict[str, dict[str, JsonType]]:
        newest: dict[str, dict[str, JsonType]] = {}

        async def on_message(message: Any) -> None:
            try:
                record = json.loads(bytes(message.value).decode("utf-8"))
                overlay_id = str(record["overlay_id"])
                loaded_at = datetime.fromisoformat(str(record["loaded_at"]))
            except (ValueError, KeyError, TypeError, UnicodeDecodeError):
                self.malformed += 1
                return
            held = newest.get(overlay_id)
            if held is None or loaded_at >= datetime.fromisoformat(
                str(held["loaded_at"])
            ):
                newest[overlay_id] = cast("dict[str, JsonType]", record)

        async with self._open_bus() as bus:
            subscribe = cast("Callable[..., Awaitable[Any]]", bus.subscribe)
            # A broker replays from the earliest retained offset; the in-memory bus takes no such policy.
            extra = (
                {"auto_offset_reset": "earliest"}
                if "auto_offset_reset" in inspect.signature(bus.subscribe).parameters
                else {}
            )
            stop = await subscribe(
                self._topic,
                on_message=on_message,
                group_id=f"{self._group_prefix}.{uuid.uuid4().hex}",
                **extra,
            )
            try:
                await asyncio.sleep(self._window_s)
            finally:
                await stop()
        return newest


__all__ = ["BusSnapshotOverlayStore", "OverlayStoreReadOnlyError"]
