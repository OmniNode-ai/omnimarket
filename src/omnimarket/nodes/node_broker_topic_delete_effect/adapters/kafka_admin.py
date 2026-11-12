# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The broker's live topic evidence and topic deletes over the Kafka admin API (OMN-23879).

The client config comes from the declared bus lane the overlay names (the same declared machine
credential ``onex scm`` uses), never from this module. What the principal may not Describe the
broker leaves out of its metadata, so a topic it cannot see reads back as not existing and is never
deleted; ``REQUIRED_ACL_OPERATIONS`` lists what a run needs to see every named topic.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from omnimarket.nodes.node_broker_topic_delete_effect.models.model_topic_delete import (
    ModelLiveTopicEvidence,
)
from omnimarket.nodes.node_broker_topic_delete_effect.overlay import (
    ModelTopicDeleteOverlay,
)
from omnimarket.nodes.node_broker_topic_delete_effect.protocols import BrokerAdminError

#: (resource type, operation) pairs the adapter exercises on the broker.
REQUIRED_ACL_OPERATIONS: tuple[tuple[str, str], ...] = (
    ("CLUSTER", "DESCRIBE"),
    ("GROUP", "DESCRIBE"),
    ("TOPIC", "DESCRIBE"),
    ("TOPIC", "READ"),
    ("TOPIC", "DELETE"),
)

ClientConfigResolver = Callable[[str], Mapping[str, str]]
AdminFactory = Callable[[Mapping[str, str]], Any]
ConsumerFactory = Callable[[Mapping[str, str]], Any]


def resolve_client_config(lane: str) -> Mapping[str, str]:
    """The declared lane's broker client config (bootstrap servers and machine credential)."""
    from omnimarket.nodes.node_runtime_coverage_effect.handlers.handler_runtime_coverage import (
        resolve_broker,
    )

    _bootstrap, _environment, config = resolve_broker(lane)
    return config


def _admin(config: Mapping[str, str]) -> Any:
    from confluent_kafka.admin import AdminClient

    return AdminClient(dict(config))


def _consumer(config: Mapping[str, str]) -> Any:
    from confluent_kafka import Consumer

    return Consumer(dict(config))


def _error_name(exc: Exception) -> str:
    error = getattr(exc, "args", [None])[0]
    code = getattr(error, "name", None)
    return (callable(code) and str(code())) or type(exc).__name__


class KafkaBrokerTopicAdmin:
    """Live evidence and deletes of named topics through one declared principal."""

    def __init__(
        self,
        overlay: ModelTopicDeleteOverlay,
        *,
        resolver: ClientConfigResolver = resolve_client_config,
        admin_factory: AdminFactory = _admin,
        consumer_factory: ConsumerFactory = _consumer,
    ) -> None:
        self._overlay = overlay
        self._resolver = resolver
        self._admin_factory = admin_factory
        self._consumer_factory = consumer_factory
        self._admin_client: Any = None
        self._config: dict[str, str] = {}

    def _client(self) -> Any:
        if self._admin_client is None:
            try:
                config = dict(self._resolver(self._overlay.bus_lane))
                self._admin_client = self._admin_factory(config)
                self._config = config
            except Exception as exc:
                raise BrokerAdminError(
                    f"broker_credential_unresolved:{type(exc).__name__}"
                ) from None
        return self._admin_client

    def _groups_by_topic(
        self, names: frozenset[str]
    ) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
        """Group ids per requested topic: by live member assignment, and by committed offset."""
        from confluent_kafka import ConsumerGroupTopicPartitions

        timeout = self._overlay.request_timeout_seconds
        admin = self._client()
        try:
            listing = admin.list_consumer_groups(request_timeout=timeout).result(
                timeout=timeout
            )
        except Exception as exc:
            raise BrokerAdminError(
                f"group_listing_failed:{type(exc).__name__}"
            ) from None
        if listing.errors:
            raise BrokerAdminError("group_listing_incomplete")
        group_ids = sorted(group.group_id for group in listing.valid)
        members: dict[str, set[str]] = {}
        committed: dict[str, set[str]] = {}
        if not group_ids:
            return members, committed
        try:
            described = admin.describe_consumer_groups(
                group_ids, request_timeout=timeout
            )
            for group_id, future in described.items():
                for member in future.result(timeout=timeout).members:
                    assignment = member.assignment
                    for partition in (
                        assignment.topic_partitions if assignment is not None else ()
                    ):
                        if partition.topic in names:
                            members.setdefault(partition.topic, set()).add(group_id)
            offsets = {
                group_id: admin.list_consumer_group_offsets(
                    [ConsumerGroupTopicPartitions(group_id)], request_timeout=timeout
                )[group_id]
                for group_id in group_ids
            }
            for group_id, future in offsets.items():
                for partition in future.result(timeout=timeout).topic_partitions:
                    if partition.topic in names:
                        committed.setdefault(partition.topic, set()).add(group_id)
        except BrokerAdminError:
            raise
        except Exception as exc:
            raise BrokerAdminError(f"group_read_failed:{type(exc).__name__}") from None
        return members, committed

    def _watermarks(
        self, consumer: Any, name: str, partitions: Sequence[int]
    ) -> tuple[int, int, int | None]:
        """High watermark total, retained records and the newest retained record's time (ms)."""
        from confluent_kafka import TopicPartition

        timeout = self._overlay.request_timeout_seconds
        high_total = 0
        retained = 0
        newest: int | None = None
        for partition in partitions:
            low, high = consumer.get_watermark_offsets(
                TopicPartition(name, partition), timeout=timeout, cached=False
            )
            high_total += high
            retained += max(high - low, 0)
            if high > low:
                consumer.assign([TopicPartition(name, partition, high - 1)])
                message = consumer.poll(timeout)
                if message is not None and message.error() is None:
                    stamp = message.timestamp()[1]
                    if stamp is not None and stamp > 0:
                        newest = stamp if newest is None else max(newest, stamp)
        return high_total, retained, newest

    def read_evidence(self, names: Sequence[str]) -> dict[str, ModelLiveTopicEvidence]:
        timeout = self._overlay.request_timeout_seconds
        admin = self._client()
        try:
            metadata = admin.list_topics(timeout=timeout)
        except Exception as exc:
            raise BrokerAdminError(f"metadata_failed:{type(exc).__name__}") from None
        requested = frozenset(names)
        members, committed = self._groups_by_topic(requested)
        consumer = self._consumer_factory(
            {
                **self._config,
                "group.id": self._overlay.probe_client_id,
                "enable.auto.commit": "false",
            }
        )
        evidence: dict[str, ModelLiveTopicEvidence] = {}
        try:
            for name in names:
                topic = metadata.topics.get(name)
                if topic is None or getattr(topic, "error", None) is not None:
                    evidence[name] = ModelLiveTopicEvidence(
                        exists=False,
                        partitions=0,
                        high_watermark_total=0,
                        retained_records=0,
                        consumer_groups=(),
                        offset_groups=(),
                        last_record_ms=None,
                    )
                    continue
                partitions = sorted(topic.partitions)
                try:
                    high, retained, newest = self._watermarks(
                        consumer, name, partitions
                    )
                except Exception as exc:
                    raise BrokerAdminError(
                        f"watermarks_failed:{type(exc).__name__}"
                    ) from None
                evidence[name] = ModelLiveTopicEvidence(
                    exists=True,
                    partitions=len(partitions),
                    high_watermark_total=high,
                    retained_records=retained,
                    consumer_groups=tuple(sorted(members.get(name, ()))),
                    offset_groups=tuple(sorted(committed.get(name, ()))),
                    last_record_ms=newest,
                )
        finally:
            consumer.close()
        return evidence

    def delete(self, names: Sequence[str]) -> dict[str, str]:
        timeout = self._overlay.request_timeout_seconds
        admin = self._client()
        try:
            futures = admin.delete_topics(list(names), request_timeout=timeout)
        except Exception as exc:
            raise BrokerAdminError(f"delete_call_failed:{type(exc).__name__}") from None
        outcome: dict[str, str] = {}
        for name in names:
            future = futures.get(name)
            if future is None:
                outcome[name] = "delete_unconfirmed"
                continue
            try:
                future.result(timeout=timeout)
                outcome[name] = ""
            except Exception as exc:
                outcome[name] = _error_name(exc)
        return outcome


__all__ = [
    "REQUIRED_ACL_OPERATIONS",
    "KafkaBrokerTopicAdmin",
    "resolve_client_config",
]
