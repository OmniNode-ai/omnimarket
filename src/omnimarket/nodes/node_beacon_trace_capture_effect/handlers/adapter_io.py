# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local complete-line reads, buffered Kafka publication, and read-only readback."""

import asyncio
import os
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from omnimarket.nodes.node_beacon_trace_capture_effect.models.model_beacon_hook_event import (
    ModelBeaconHookEvent,
)
from omnimarket.topic_namespace import apply_topic_namespace


def publish_topics() -> tuple[str, ...]:
    """Read the node contract as the sole publication-topic authority."""
    contract = yaml.safe_load(
        (Path(__file__).parent.parent / "contract.yaml").read_text(encoding="utf-8")
    )
    return tuple(str(topic) for topic in contract["event_bus"]["publish_topics"])


class FileBeaconLogReader:
    """Checkpoint bytes only after a complete newline-terminated record."""

    def read_from(self, path: str, offset: int) -> tuple[list[str], int]:
        if offset < 0:
            raise ValueError("offset must be non-negative")
        lines: list[str] = []
        with Path(path).open("rb") as stream:
            stream.seek(offset)
            next_offset = offset
            while line := stream.readline():
                if not line.endswith(b"\n"):
                    break
                lines.append(line.rstrip(b"\r\n").decode("utf-8"))
                next_offset = stream.tell()
        return lines, next_offset


class KafkaHookEventPublisher:
    """Buffer one capture and flush with the shared Kafka auth and namespace."""

    def __init__(self, bus_lane: str | None = None) -> None:
        self._buffer: list[tuple[str, str, ModelBeaconHookEvent]] = []
        self._bus_lane = bus_lane

    def publish(self, topic: str, key: str, event: ModelBeaconHookEvent) -> None:
        if topic not in publish_topics():
            raise ValueError("undeclared publish topic")
        self._buffer.append((topic, key, event))

    def flush(self) -> None:
        """Return only once every buffered event has been acknowledged."""
        if not self._buffer:
            return
        if self._bus_lane:
            # A named bus lane resolves its broker and the host's own client
            # identity the way the onex CLI does, never from this environment.
            from omnibase_infra.cli.delegate_lane_credentials import (
                resolve_lane_binding,
            )
            from omnibase_infra.event_bus.lane_client_transport_binding import (
                bind_lane_client_transport,
            )

            selection, transport = resolve_lane_binding(self._bus_lane)
            brokers = selection.bootstrap_servers
            if transport is not None:
                with bind_lane_client_transport(transport):
                    asyncio.run(self._publish_kafka(brokers))
            else:
                asyncio.run(self._publish_kafka(brokers))
        else:
            brokers = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "").strip()
            if not brokers:
                raise RuntimeError("KAFKA_BOOTSTRAP_SERVERS must be configured")
            asyncio.run(self._publish_kafka(brokers))
        self._buffer.clear()

    async def _publish_kafka(self, brokers: str) -> None:
        from aiokafka import AIOKafkaProducer
        from omnibase_infra.event_bus.kafka_auth import (
            build_aiokafka_auth_kwargs_for,
        )

        # The lane-aware resolver supplies the auth kwargs; the protocol is
        # stated on the call so a plaintext answer is explicit, not implied.
        auth = build_aiokafka_auth_kwargs_for(brokers)
        security_protocol = str(auth.pop("security_protocol", "PLAINTEXT"))
        producer = AIOKafkaProducer(
            bootstrap_servers=brokers, security_protocol=security_protocol, **auth
        )
        try:
            await producer.start()
            for topic, key, event in self._buffer:
                if topic not in publish_topics():
                    raise ValueError("undeclared publish topic")
                await producer.send_and_wait(
                    apply_topic_namespace(topic),
                    event.model_dump_json().encode("utf-8"),
                    key=key.encode("utf-8"),
                )
        finally:
            await producer.stop()


class ProjectionReadback:
    """Read only the projection rows corresponding to this batch's event IDs."""

    def __init__(self, dsn: str | None = None) -> None:
        # Let the adapter resolve its default DSN.
        self._dsn = dsn or ""

    async def read(self, event_ids: tuple[UUID, ...]) -> list[dict[str, Any]]:
        if not event_ids:
            return []
        from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter

        adapter = AsyncpgAdapter(dsn=self._dsn, min_size=1, max_size=1)
        try:
            await adapter.connect()
            return await adapter.execute(
                "select event_id, session_id, hook_event_name, lane, emitted_at "
                "from omninode_internal.claude_hook_events "
                "where event_id = any($1::uuid[]) order by emitted_at",
                list(event_ids),
            )
        finally:
            await adapter.close()
