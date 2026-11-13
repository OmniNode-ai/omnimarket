# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tick orchestrator's Redpanda publisher for its compared and compare-failed events (OMN-23539).

Each publish opens the overlay's declared bus lane (the broker and the client transport resolve from
the workspace's lane config under the workspace root, as every deployed node's bus does), or the
plaintext lab broker the overlay names in place of a lane, sends the event's JSON on the topic under
the namespace, keyed by the tick, and waits for the broker's acknowledgement.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from pydantic import BaseModel

from omnimarket.topic_namespace import apply_topic_namespace


class BusTickPublisher:
    """ProtocolTickPublisher over the overlay's declared bus lane or lab broker (OMN-23539)."""

    def __init__(
        self,
        *,
        bus_lane: str | None,
        workspace_root: str | None,
        bootstrap_servers: str | None,
    ) -> None:
        self._lane = bus_lane
        self._root = workspace_root
        self._bootstrap = bootstrap_servers

    def publish(self, topic: str, event: BaseModel) -> None:
        asyncio.run(self._send(topic, event))

    async def _send(self, topic: str, event: BaseModel) -> None:
        from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus

        root = (
            Path(os.path.expanduser(os.path.expandvars(self._root)))
            if self._root
            else None
        )
        async with open_lane_bus(
            bus="kafka",
            lane=self._lane,
            kafka_bootstrap=None if self._lane else self._bootstrap,
            root=root,
        ) as bus:
            await bus.publish(
                apply_topic_namespace(topic),
                _key(event),
                event.model_dump_json().encode(),
            )


def _key(event: BaseModel) -> bytes:
    payload = event.model_dump(mode="json")
    return str(payload.get("tick", payload.get("correlation_id"))).encode()


__all__: list[str] = ["BusTickPublisher"]
