# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bus adapters of the stability lane refresh serve tick: the request reader and the event publisher.

The reader opens the overlay's declared bus lane (the broker and the client transport resolve from the
lane config under the workspace root, as every deployed node's bus does), subscribes to the request topic
the contract names under the overlay's consumer group from the earliest uncommitted record, and collects
up to the overlay's record cap of raw request strings within its read timeout; a group join slower than
the overlay's join timeout raises :class:`StabilityServeJoinTimeoutError`. The publisher sends each
answer event on its topic under the namespace, keyed by the correlation id. Neither hard-codes a broker
address, port, group or timeout: every one comes from the overlay.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_stability_lane_refresh_effect.overlay import (
    ModelStabilityServeOverlay,
)
from omnimarket.topic_namespace import apply_topic_namespace


class StabilityServeJoinTimeoutError(Exception):
    """The consumer group join took longer than the overlay's join_timeout_s: the pass ends unread."""


class BusRequestReader:
    """Reads the raw request strings of the request topic from the overlay's declared bus lane."""

    def __init__(self, serve: ModelStabilityServeOverlay) -> None:
        self._serve = serve

    def read(self, topic: str) -> list[str]:
        return asyncio.run(self._read(topic))

    async def _read(self, topic: str) -> list[str]:
        from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus

        serve = self._serve
        root = Path(os.path.expanduser(os.path.expandvars(serve.workspace_root)))
        async with open_lane_bus(
            bus="kafka", lane=serve.bus_lane, kafka_bootstrap=None, root=root
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
                raise StabilityServeJoinTimeoutError(
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


class BusEventPublisher:
    """Publishes the serve tick's answer events back on the bus, keyed by the correlation id."""

    def __init__(self, serve: ModelStabilityServeOverlay) -> None:
        self._serve = serve

    def publish(self, topic: str, body_bytes: bytes, key: str | None = None) -> None:
        asyncio.run(self._send(topic, body_bytes, key))

    async def _send(self, topic: str, body_bytes: bytes, key: str | None) -> None:
        from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus

        serve = self._serve
        root = Path(os.path.expanduser(os.path.expandvars(serve.workspace_root)))
        async with open_lane_bus(
            bus="kafka", lane=serve.bus_lane, kafka_bootstrap=None, root=root
        ) as bus:
            await bus.publish(
                apply_topic_namespace(topic),
                key.encode() if key is not None else None,
                body_bytes,
            )


__all__ = [
    "BusEventPublisher",
    "BusRequestReader",
    "StabilityServeJoinTimeoutError",
]
