"""Kafka/Redpanda topic probe — lists topics and latest offsets via admin API."""

from __future__ import annotations

import logging
import os

from omnimarket.nodes.node_baseline_capture.models.model_baseline import (
    ModelKafkaTopicSnapshot,
    ProbeSnapshotItem,
)

logger = logging.getLogger(__name__)


class ProbeKafkaTopics:
    """Probe that collects Kafka/Redpanda topic metadata via the admin HTTP API."""

    name: str = "kafka_topics"

    async def collect(self, omni_home: str) -> list[ProbeSnapshotItem]:
        """Collect topic list from the Redpanda admin API.

        Falls back to empty list on any failure — probe errors are non-fatal.
        """
        try:
            import httpx
        except ImportError:
            logger.warning("httpx not available — skipping kafka_topics probe")
            return []

        # Redpanda admin API — default port 9644
        admin_host = os.environ.get("REDPANDA_ADMIN_HOST", "192.168.86.201")
        admin_port = os.environ.get("REDPANDA_ADMIN_PORT", "9644")
        base_url = f"http://{admin_host}:{admin_port}"

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                topics_resp = await client.get(f"{base_url}/v1/topics")
                topics_resp.raise_for_status()
                topics: list[dict[str, object]] = topics_resp.json()
        except Exception as exc:
            logger.warning("Redpanda admin API unavailable: %s", exc)
            return []

        results: list[ProbeSnapshotItem] = []
        for topic_obj in topics:
            topic_name = str(topic_obj.get("name", ""))
            if not topic_name or topic_name.startswith("_"):
                # Skip internal topics
                continue

            partitions = topic_obj.get("partitions", [])
            partition_count = len(partitions) if isinstance(partitions, list) else 0

            # Sum latest offsets across all partitions
            latest_offset = 0
            if isinstance(partitions, list):
                for part in partitions:
                    latest_offset += int(part.get("latest_offset", 0))

            results.append(
                ModelKafkaTopicSnapshot(
                    topic=topic_name,
                    partition_count=partition_count,
                    latest_offset=latest_offset,
                )
            )

        return results


__all__: list[str] = ["ProbeKafkaTopics"]
