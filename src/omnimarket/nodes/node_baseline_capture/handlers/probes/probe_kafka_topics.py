"""ProbeKafkaTopics — list Kafka/Redpanda topics and latest offsets via kcat."""

from __future__ import annotations

import json
import logging
import os
import subprocess

from omnimarket.nodes.node_baseline_capture.models.model_baseline import (
    ModelKafkaTopicSnapshot,
)

logger = logging.getLogger(__name__)

_KCAT_TIMEOUT_SECONDS = 15
_DEFAULT_BROKER = "192.168.86.201:19092"


def _parse_kcat_metadata(raw: str) -> list[ModelKafkaTopicSnapshot]:
    """Parse kcat -L -J JSON output into topic snapshots."""
    try:
        data: dict[str, object] = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("probe_kafka_topics: failed to parse kcat JSON: %s", exc)
        return []

    topics_raw = data.get("topics", [])
    if not isinstance(topics_raw, list):
        return []

    snapshots: list[ModelKafkaTopicSnapshot] = []
    for topic_obj in topics_raw:
        if not isinstance(topic_obj, dict):
            continue
        topic_name = str(topic_obj.get("topic", ""))
        if not topic_name or topic_name.startswith("__"):
            # Skip internal topics
            continue

        partitions = topic_obj.get("partitions", [])
        partition_count = len(partitions) if isinstance(partitions, list) else 0

        latest_offset = 0
        if isinstance(partitions, list):
            for partition in partitions:
                if isinstance(partition, dict):
                    # kcat -L -J exposes hi_offset as the high watermark
                    hi = partition.get("hi_offset", 0)
                    if isinstance(hi, int) and hi > latest_offset:
                        latest_offset = hi

        snapshots.append(
            ModelKafkaTopicSnapshot(
                topic=topic_name,
                partition_count=partition_count,
                latest_offset=latest_offset,
            )
        )

    return snapshots


class ProbeKafkaTopics:
    """List Kafka topics and partition info via kcat CLI."""

    name: str = "kafka_topics"

    async def collect(self) -> list[ModelKafkaTopicSnapshot]:
        """Return topic snapshots; returns empty list on any failure."""
        broker = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", _DEFAULT_BROKER)

        try:
            result = subprocess.run(
                ["kcat", "-b", broker, "-L", "-J"],
                capture_output=True,
                text=True,
                timeout=_KCAT_TIMEOUT_SECONDS,
                check=False,
            )
            if result.returncode != 0:
                logger.warning(
                    "probe_kafka_topics: kcat exited %d: %s",
                    result.returncode,
                    result.stderr.strip(),
                )
                return []

            return _parse_kcat_metadata(result.stdout)

        except subprocess.TimeoutExpired:
            logger.warning(
                "probe_kafka_topics: kcat timed out after %ds", _KCAT_TIMEOUT_SECONDS
            )
            return []
        except (OSError, ValueError) as exc:
            logger.warning("probe_kafka_topics failed: %s", exc)
            return []


__all__: list[str] = ["ProbeKafkaTopics"]
