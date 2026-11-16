# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host disk reading's publisher on the workspace's declared bus lane (OMN-23865).

Sends an event's JSON on its topic, keyed by the host, and waits for the broker's acknowledgement, so a reading
reported published is on the broker. The broker and the client transport resolve from the workspace's lane config
under the workspace root, as every deployed node's bus does.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel

from omnimarket.topic_namespace import apply_topic_namespace


class LaneBusHostDiskPublisher:
    """ProtocolHostDiskPublisher on the workspace's bus lane."""

    def __init__(self, bus_lane: str, workspace_root: Path | None) -> None:
        self._lane = bus_lane
        self._root = workspace_root

    def publish(self, topic: str, event: BaseModel) -> None:
        asyncio.run(self._send(topic, event))

    async def _send(self, topic: str, event: BaseModel) -> None:
        from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus

        payload = event.model_dump(mode="json")
        async with open_lane_bus(
            bus="kafka", lane=self._lane, kafka_bootstrap=None, root=self._root
        ) as bus:
            await bus.publish(
                apply_topic_namespace(topic),
                str(payload["host_name"]).encode(),
                event.model_dump_json().encode(),
            )


__all__: list[str] = ["LaneBusHostDiskPublisher"]
