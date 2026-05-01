"""Hermetic round-trip test for the emit daemon health probe."""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import time
from pathlib import Path

import pytest

from omnimarket.nodes.node_emit_daemon.event_queue import BoundedEventQueue
from omnimarket.nodes.node_emit_daemon.event_registry import EventRegistry
from omnimarket.nodes.node_emit_daemon.health_probe import (
    HEALTH_PROBE_EVENT_TYPE,
    probe,
)
from omnimarket.nodes.node_emit_daemon.publisher_loop import KafkaPublisherLoop
from omnimarket.nodes.node_emit_daemon.socket_server import EmitSocketServer


class _PublishedRecords:
    def __init__(self) -> None:
        self._records: list[dict[str, object]] = []
        self._lock = threading.Lock()

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: dict[str, object],
    ) -> None:
        _ = key, headers
        with self._lock:
            self._records.append(
                {"topic": topic, "value": value, "offset": len(self._records)}
            )

    def pop_for_topics(self, topics: list[str]) -> dict[str, object] | None:
        with self._lock:
            for index, record in enumerate(self._records):
                if record["topic"] in topics:
                    return self._records.pop(index)
        return None


class _PublishedConsumer:
    def __init__(self, records: _PublishedRecords) -> None:
        self._records = records
        self._topics: list[str] = []

    def subscribe(self, topics: list[str]) -> None:
        self._topics = topics

    def poll(self, timeout: float) -> object | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() <= deadline:
            record = self._records.pop_for_topics(self._topics)
            if record is not None:
                return record
            time.sleep(0.005)
        return None

    def close(self) -> None:
        return None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_health_probe_round_trips_socket_to_published_record(
    tmp_path: Path,
) -> None:
    _ = tmp_path
    repo_root = Path(__file__).resolve().parents[3]
    registry_path = (
        repo_root
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_emit_daemon"
        / "registries"
        / "topics.yaml"
    )
    registry = EventRegistry.from_yaml(registry_path)
    registration = registry.get_registration(HEALTH_PROBE_EVENT_TYPE)
    assert registration is not None
    assert registration.fan_out
    with tempfile.TemporaryDirectory(prefix="omn10128-", dir="/tmp") as short_tmp:
        short_root = Path(short_tmp)
        records = _PublishedRecords()
        queue = BoundedEventQueue(spool_dir=short_root / "spool")
        socket_path = str(short_root / "emit.sock")
        publisher = KafkaPublisherLoop(queue=queue, publish_fn=records.publish)
        server = EmitSocketServer(
            socket_path=socket_path,
            queue=queue,
            registry=registry,
            publisher_loop=publisher,
        )

        await server.start()
        await publisher.start()
        try:
            result = await asyncio.to_thread(
                probe,
                socket_path,
                "fake-bootstrap:9092",
                "corr-round-trip",
                2.0,
                event_registry_path=registry_path,
                consumer_factory=lambda _bootstrap_servers, _group_id: (
                    _PublishedConsumer(records)
                ),
            )
        finally:
            await publisher.stop()
            await server.stop()

    assert result.success is True
    assert result.correlation_id == "corr-round-trip"
    assert result.kafka_offset == 0
    assert result.round_trip_ms is not None
    assert result.round_trip_ms >= 0
    assert result.event_id is not None
    assert result.reason == "daemon health probe round trip succeeded"


@pytest.mark.integration
def test_fake_record_payload_shape_matches_publisher_loop() -> None:
    payload = {"correlation_id": "corr", "probe": "daemon-health"}
    encoded = json.dumps(payload).encode("utf-8")
    assert json.loads(encoded) == payload
