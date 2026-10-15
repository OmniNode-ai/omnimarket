# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The production end of the consumer-group source: a read-only broker binding.

Reads committed offsets with the admin client and end offsets with a consumer
that never subscribes, joins a group or commits, so reading a group moves
nothing. Any broker error is raised; the source reports the entry UNOBSERVABLE.
"""

from confluent_kafka import Consumer, ConsumerGroupTopicPartitions, TopicPartition
from confluent_kafka.admin import AdminClient

from omnimarket.nodes.node_automation_run_observer_effect.sources.source_consumer_group import (
    ModelConsumerGroupSnapshot,
    ModelPartitionOffsets,
)

_TIMEOUT_SECONDS = 15.0
#: Names the read-only consumer; it never joins or commits under this id.
_READER_GROUP = "onex-automation-run-observer-readonly"


class KafkaConsumerGroupReader:
    def read(
        self, bootstrap: str, group: str, completion_topic: str | None
    ) -> ModelConsumerGroupSnapshot:
        admin = AdminClient({"bootstrap.servers": bootstrap})
        future = admin.list_consumer_group_offsets(
            [ConsumerGroupTopicPartitions(group)], request_timeout=_TIMEOUT_SECONDS
        )[group]
        committed = future.result(timeout=_TIMEOUT_SECONDS).topic_partitions
        consumer = Consumer(
            {
                "bootstrap.servers": bootstrap,
                "group.id": _READER_GROUP,
                "enable.auto.commit": False,
            }
        )
        try:
            partitions = tuple(
                ModelPartitionOffsets(
                    topic=tp.topic,
                    partition=tp.partition,
                    end_offset=_end_offset(consumer, tp.topic, tp.partition),
                    committed_offset=tp.offset if tp.offset >= 0 else None,
                )
                for tp in committed
            )
            total = (
                _completions(consumer, completion_topic)
                if completion_topic is not None
                else None
            )
        finally:
            consumer.close()
        return ModelConsumerGroupSnapshot(
            partitions=partitions, completions_total=total
        )


def _end_offset(consumer: Consumer, topic: str, partition: int) -> int:
    _, high = consumer.get_watermark_offsets(
        TopicPartition(topic, partition), timeout=_TIMEOUT_SECONDS
    )
    return int(high)


def _completions(consumer: Consumer, topic: str) -> int:
    metadata = consumer.list_topics(topic, timeout=_TIMEOUT_SECONDS)
    found = metadata.topics.get(topic)
    if found is None or found.error is not None:
        raise LookupError(f"completion topic {topic} is not on the broker")
    return sum(_end_offset(consumer, topic, p) for p in found.partitions)
