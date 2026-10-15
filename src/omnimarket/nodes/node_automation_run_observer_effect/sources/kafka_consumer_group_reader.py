# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The production end of the consumer-group source: a read-only broker binding.

Reads committed offsets, end offsets and topic metadata with the admin client
alone: no consumer is built, so nothing subscribes, joins a group or commits,
and reading a group moves nothing. Any broker error is raised; the source
reports the entry UNOBSERVABLE.
"""

from collections.abc import Callable

from confluent_kafka import ConsumerGroupTopicPartitions, TopicPartition
from confluent_kafka.admin import AdminClient

# The class confluent_kafka.admin re-exports, from the module that types it.
from confluent_kafka.admin._listoffsets import OffsetSpec

from omnimarket.nodes.node_automation_run_observer_effect.sources.source_consumer_group import (
    ModelConsumerGroupSnapshot,
    ModelPartitionOffsets,
)

_TIMEOUT_SECONDS = 15.0
#: OffsetSpec.latest is untyped upstream; bound once to its real signature.
_LATEST: Callable[[], OffsetSpec] = OffsetSpec.latest


class KafkaConsumerGroupReader:
    def read(
        self, bootstrap: str, group: str, completion_topic: str | None
    ) -> ModelConsumerGroupSnapshot:
        admin = AdminClient({"bootstrap.servers": bootstrap})
        future = admin.list_consumer_group_offsets(
            [ConsumerGroupTopicPartitions(group)], request_timeout=_TIMEOUT_SECONDS
        )[group]
        committed = future.result(timeout=_TIMEOUT_SECONDS).topic_partitions
        ends = _end_offsets(admin, [(tp.topic, tp.partition) for tp in committed])
        partitions = tuple(
            ModelPartitionOffsets(
                topic=tp.topic,
                partition=tp.partition,
                end_offset=ends[(tp.topic, tp.partition)],
                committed_offset=tp.offset if tp.offset >= 0 else None,
            )
            for tp in committed
        )
        total = (
            _completions(admin, completion_topic)
            if completion_topic is not None
            else None
        )
        return ModelConsumerGroupSnapshot(
            partitions=partitions, completions_total=total
        )


def _end_offsets(
    admin: AdminClient, keys: list[tuple[str, int]]
) -> dict[tuple[str, int], int]:
    if not keys:
        return {}
    futures = admin.list_offsets(
        {TopicPartition(topic, p): _LATEST() for topic, p in keys},
        request_timeout=_TIMEOUT_SECONDS,
    )
    return {
        (tp.topic, tp.partition): int(f.result(timeout=_TIMEOUT_SECONDS).offset)
        for tp, f in futures.items()
    }


def _completions(admin: AdminClient, topic: str) -> int:
    # Whole-cluster metadata: a request naming the topic would create it on a
    # broker that auto-creates topics.
    metadata = admin.list_topics(timeout=_TIMEOUT_SECONDS)
    found = metadata.topics.get(topic)
    if found is None or found.error is not None:
        raise LookupError(f"completion topic {topic} is not on the broker")
    return sum(_end_offsets(admin, [(topic, p) for p in found.partitions]).values())
