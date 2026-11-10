# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing snapshot step's Redpanda publishers (OMN-23409).

Each sends an event's JSON on its topic, keyed by the tick, and waits for the broker's
acknowledgement, so a tick reported recorded is on the broker.

- LaneBusSnapshotPublisher: the overlay names a bus lane (dev); the broker and the client
  transport resolve from the workspace's lane config under the workspace root, as every deployed node's bus does.
- KafkaSnapshotPublisher: the overlay names a plaintext broker address (a lab broker started for a
  proof); auth, if any, from the environment's declared Kafka auth settings.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel

from omnimarket.topic_namespace import apply_topic_namespace


class KafkaSnapshotPublisher:
    """ProtocolLandingSnapshotPublisher on a Redpanda broker."""

    def __init__(self, bootstrap_servers: str) -> None:
        self._bootstrap = bootstrap_servers

    def publish(self, topic: str, event: BaseModel) -> None:
        asyncio.run(self._send(topic, event))

    async def _send(self, topic: str, event: BaseModel) -> None:
        from aiokafka import AIOKafkaProducer
        from omnibase_infra.event_bus.kafka_auth import (
            build_aiokafka_auth_kwargs_from_env,
        )

        producer = AIOKafkaProducer(
            bootstrap_servers=self._bootstrap, **build_aiokafka_auth_kwargs_from_env()
        )
        await producer.start()
        try:
            await producer.send_and_wait(
                apply_topic_namespace(topic),
                event.model_dump_json().encode(),
                key=_key(event),
            )
        finally:
            await producer.stop()


def _key(event: BaseModel) -> bytes:
    payload = event.model_dump(mode="json")
    return str(payload.get("tick", payload.get("correlation_id"))).encode()


class LaneBusSnapshotPublisher:
    """ProtocolLandingSnapshotPublisher on the workspace's bus lane."""

    def __init__(self, bus_lane: str, workspace_root: Path) -> None:
        self._lane = bus_lane
        self._root = workspace_root

    def publish(self, topic: str, event: BaseModel) -> None:
        asyncio.run(self._send(topic, event))

    async def _send(self, topic: str, event: BaseModel) -> None:
        from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus

        async with open_lane_bus(
            bus="kafka", lane=self._lane, kafka_bootstrap=None, root=self._root
        ) as bus:
            await bus.publish(
                apply_topic_namespace(topic),
                _key(event),
                event.model_dump_json().encode(),
            )


__all__: list[str] = ["KafkaSnapshotPublisher", "LaneBusSnapshotPublisher"]
