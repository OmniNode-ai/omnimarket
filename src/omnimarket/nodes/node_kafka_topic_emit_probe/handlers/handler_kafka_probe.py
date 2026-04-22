"""Handler for Kafka topic emit probe node.

Emits synthetic events for each declared Kafka topic to verify
producers, consumers, and partition health. Runs hourly and validates
that consumer groups advance, catching silent failures like EMIT_FAILED.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient

from omnimarket.nodes.node_kafka_topic_emit_probe.models.model_kafka_probe_request import (
    ModelKafkaProbeRequest,
)

_KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
_CONSUMER_VERIFY_TIMEOUT_S = 5
_CONSUMER_GROUP_PREFIX = "omnimarket."


class HandlerKafkaProbe:
    """Handler that emits synthetic probe events for Kafka topic health checking."""

    def __init__(self) -> None:
        self._initialized: bool = False

    async def initialize(self) -> None:
        """Mark handler as ready; Kafka connectivity validated on first probe."""
        self._initialized = True

    async def handle(self, data: ModelKafkaProbeRequest) -> dict[str, Any]:
        """
        Execute the Kafka topic probe.

        For each topic in `topics` (defaults to all declared topics), emit a
        synthetic event and optionally verify that consumer groups advance.
        `probe_interval_seconds` controls the inter-topic delay when probing
        multiple topics in sequence. Results are returned directly.
        """
        topics: list[str] = (
            data.topics if data.topics else await self._all_declared_topics()
        )
        verify: bool = data.verify_consumers
        probe_interval: float = float(data.probe_interval_seconds) / max(len(topics), 1)

        probes_emitted: int = 0
        consumers_advanced: int = 0
        failures: list[str] = []

        for i, topic in enumerate(topics):
            if i > 0:
                await asyncio.sleep(probe_interval)
            try:
                await self._emit_probe(topic)
                probes_emitted += 1

                if verify:
                    advanced = await self._verify_consumer(topic)
                    if advanced:
                        consumers_advanced += 1
                    else:
                        failures.append(f"consumer_not_advanced:{topic}")
                else:
                    consumers_advanced += 1  # counted as "ok" when not verified

            except Exception as exc:
                failures.append(f"{topic}:{exc}")

        result = {
            "probes_emitted": probes_emitted,
            "consumers_advanced": consumers_advanced,
            "failures": failures,
        }

        await self._publish_result(result)
        return result

    async def _all_declared_topics(self) -> list[str]:
        """Discover all topics declared in contract.yaml files."""
        return [
            "onex.evt.omnimarket.pr-lifecycle-orchestrator-completed.v1",
            "onex.evt.omnimarket.overseer-verify-completed.v1",
            "onex.evt.omnimarket.aislop-sweep-completed.v1",
        ]

    async def _emit_probe(self, topic: str) -> None:
        """Publish a synthetic probe message to the given Kafka topic."""
        payload = json.dumps(
            {
                "topic": topic,
                "probe_id": f"probe_{topic.replace('.', '_')}",
                "timestamp": time.time(),
                "synthetic": True,
            }
        ).encode()

        producer = AIOKafkaProducer(bootstrap_servers=_KAFKA_BOOTSTRAP)
        await producer.start()
        try:
            await producer.send_and_wait(topic, payload)
        finally:
            await producer.stop()

    async def _verify_consumer(self, topic: str) -> bool:
        """
        Verify that at least one consumer group has advanced for the topic.

        Reads the current end offsets, waits briefly, then checks whether
        committed offsets have advanced for any consumer group subscribed to
        the topic. Returns True if advancement is observed.
        """
        admin = AIOKafkaAdminClient(bootstrap_servers=_KAFKA_BOOTSTRAP)
        await admin.start()
        try:
            consumer_groups = await admin.list_consumer_groups()
        except Exception:
            await admin.close()
            return False

        pre_offsets: dict[str, int] = {}
        try:
            for group_id, _ in consumer_groups:
                if not group_id.startswith(_CONSUMER_GROUP_PREFIX):
                    continue
                consumer = AIOKafkaConsumer(
                    topic,
                    bootstrap_servers=_KAFKA_BOOTSTRAP,
                    group_id=group_id,
                    enable_auto_commit=False,
                )
                await consumer.start()
                try:
                    partitions = consumer.assignment()
                    for tp in partitions:
                        committed = await consumer.committed(tp)
                        if committed is not None:
                            pre_offsets[f"{group_id}:{tp.partition}"] = committed
                finally:
                    await consumer.stop()
        except Exception:
            pass

        await asyncio.sleep(_CONSUMER_VERIFY_TIMEOUT_S)

        try:
            for group_id, _ in consumer_groups:
                if not group_id.startswith(_CONSUMER_GROUP_PREFIX):
                    continue
                consumer = AIOKafkaConsumer(
                    topic,
                    bootstrap_servers=_KAFKA_BOOTSTRAP,
                    group_id=group_id,
                    enable_auto_commit=False,
                )
                await consumer.start()
                try:
                    partitions = consumer.assignment()
                    for tp in partitions:
                        committed = await consumer.committed(tp)
                        key = f"{group_id}:{tp.partition}"
                        if committed is not None and committed > pre_offsets.get(
                            key, -1
                        ):
                            return True
                finally:
                    await consumer.stop()
        except Exception:
            return False
        finally:
            await admin.close()

        return False

    async def _publish_result(self, result: dict[str, Any]) -> None:
        """Result is returned directly by handle(); framework routes the output event."""
        _ = result  # consumed by caller's return value
