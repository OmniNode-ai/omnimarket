# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bus adapters of the collector's serve tick: the command reader and the answer publisher.

The reader opens the overlay's declared bus lane (the broker and the client transport resolve from the lane config under
the workspace root, as every deployed node's bus does), subscribes to the command topic under the overlay's consumer
group from the earliest uncommitted record, and collects up to the overlay's record cap of raw command strings within
its read timeout; a group join slower than the overlay's join timeout raises :class:`CollectServeJoinTimeoutError`. The
publisher sends each answer on its topic, keyed by the correlation id. Every address, group and bound comes from the
overlay, none from code.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_test_selector_miss_collect_effect.models.model_miss_collect import (
    ModelCollectServeOverlay,
)
from omnimarket.topic_namespace import apply_topic_namespace


class CollectServeJoinTimeoutError(Exception):
    """The consumer group join took longer than the overlay's join_timeout_s: the pass ends unread."""


def _root(serve: ModelCollectServeOverlay) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(serve.workspace_root)))


class BusCommandReader:
    """Reads the raw command strings of the command topic from the overlay's declared bus lane."""

    def __init__(self, serve: ModelCollectServeOverlay) -> None:
        self._serve = serve

    def read(self, topic: str) -> list[str]:
        return asyncio.run(self._read(topic))

    async def _read(self, topic: str) -> list[str]:
        from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus

        serve = self._serve
        async with open_lane_bus(
            bus="kafka",
            lane=serve.bus_lane,
            kafka_bootstrap=None,
            root=_root(serve),
        ) as bus:
            raw: list[str] = []
            done = asyncio.Event()

            async def collect(message: Any) -> None:
                if len(raw) < serve.max_records:
                    raw.append(bytes(message.value).decode())
                if len(raw) >= serve.max_records:
                    done.set()

            try:
                handle = await asyncio.wait_for(
                    bus.subscribe(
                        apply_topic_namespace(topic),
                        on_message=collect,
                        group_id=serve.consumer_group,
                        auto_offset_reset="earliest",
                    ),
                    serve.join_timeout_s,
                )
            except TimeoutError:
                raise CollectServeJoinTimeoutError(
                    f"the consumer group {serve.consumer_group} did not join "
                    f"within {serve.join_timeout_s} s"
                ) from None
            try:
                await asyncio.wait_for(done.wait(), serve.read_timeout_s)
            except TimeoutError:
                pass
            finally:
                await handle.stop()
        return raw


class BusAnswerPublisher:
    """Publishes the serve tick's answer back on the bus, keyed by the correlation id."""

    def __init__(self, serve: ModelCollectServeOverlay) -> None:
        self._serve = serve

    def publish(self, topic: str, body_bytes: bytes, key: str | None = None) -> None:
        asyncio.run(self._send(topic, body_bytes, key))

    async def _send(self, topic: str, body_bytes: bytes, key: str | None) -> None:
        from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus

        serve = self._serve
        async with open_lane_bus(
            bus="kafka",
            lane=serve.bus_lane,
            kafka_bootstrap=None,
            root=_root(serve),
        ) as bus:
            await bus.publish(
                apply_topic_namespace(topic),
                key.encode() if key is not None else None,
                body_bytes,
            )


__all__: list[str] = [
    "BusAnswerPublisher",
    "BusCommandReader",
    "CollectServeJoinTimeoutError",
]
