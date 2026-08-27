# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit coverage for explicit Kafka smoke-topic lifecycle management."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from aiokafka.admin import NewTopic

from tests.helpers.kafka_topic_lifecycle import (
    KafkaAdminClient,
    KafkaAdminFactory,
    KafkaSmokeTopicIdentity,
    KafkaSmokeTopicLease,
)


@dataclass
class _FakeAdmin:
    """Typed admin double that models create, metadata, and delete calls."""

    create_succeeds: bool = True
    metadata_ready: bool = True
    live_topics: set[str] = field(default_factory=set)
    created_topics: list[list[str]] = field(default_factory=list)
    deleted_topics: list[list[str]] = field(default_factory=list)
    describe_calls: list[list[str]] = field(default_factory=list)
    started: bool = False
    closed: bool = False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def create_topics(self, topics: list[NewTopic]) -> object:
        names = [topic.name for topic in topics]
        self.created_topics.append(names)
        if not self.create_succeeds:
            return SimpleNamespace(topic_errors=[(names[0], 36, "already exists")])
        self.live_topics.update(names)
        return SimpleNamespace(topic_errors=[(name, 0, "") for name in names])

    async def delete_topics(self, topics: list[str]) -> object:
        self.deleted_topics.append(list(topics))
        self.live_topics.difference_update(topics)
        return SimpleNamespace(topic_error_codes=[(topic, 0) for topic in topics])

    async def describe_topics(self, topics: list[str]) -> object:
        self.describe_calls.append(list(topics))
        descriptions: list[Mapping[str, object]] = []
        for topic in topics:
            if topic not in self.live_topics:
                continue
            descriptions.append(
                {
                    "topic": topic,
                    "error_code": 0,
                    "partitions": [{"partition": 0}] if self.metadata_ready else [],
                }
            )
        return descriptions


def _factory_for(
    admin: _FakeAdmin,
) -> tuple[list[dict[str, object]], KafkaAdminFactory]:
    calls: list[dict[str, object]] = []

    def factory(**kwargs: object) -> KafkaAdminClient:
        calls.append(dict(kwargs))
        return admin

    return calls, factory


def test_topic_and_group_identity_is_unique_and_versioned() -> None:
    first = KafkaSmokeTopicIdentity()
    second = KafkaSmokeTopicIdentity()

    assert first.topic != second.topic
    assert first.group_id != second.group_id
    assert first.topic.startswith("onex.evt.omnimarket.integration-kafka-smoke-")
    assert first.topic.endswith(".v1")
    assert first.group_id.startswith("omnimarket-integration-smoke-")


@pytest.mark.asyncio
async def test_lease_deletes_only_its_created_topic() -> None:
    admin = _FakeAdmin(live_topics={"unowned-topic"})
    factory_calls, factory = _factory_for(admin)
    lease = KafkaSmokeTopicLease(
        bootstrap_servers="broker:9092",
        auth_kwargs={"security_protocol": "SASL_SSL"},
        admin_factory=factory,
        metadata_poll_interval_seconds=0.001,
    )

    async with lease:
        assert admin.started is True
        assert admin.created_topics == [[lease.topic]]
        assert admin.deleted_topics == []
        assert "unowned-topic" in admin.live_topics

    assert admin.deleted_topics == [[lease.topic]]
    assert admin.live_topics == {"unowned-topic"}
    assert admin.closed is True
    assert factory_calls == [
        {
            "bootstrap_servers": "broker:9092",
            "security_protocol": "SASL_SSL",
        }
    ]


@pytest.mark.asyncio
async def test_parallel_leases_keep_topic_cleanup_disjoint() -> None:
    admin = _FakeAdmin(live_topics={"unowned-topic"})
    _factory_calls, factory = _factory_for(admin)
    first = KafkaSmokeTopicLease(
        bootstrap_servers="broker:9092",
        auth_kwargs={},
        admin_factory=factory,
        metadata_poll_interval_seconds=0.001,
    )
    second = KafkaSmokeTopicLease(
        bootstrap_servers="broker:9092",
        auth_kwargs={},
        admin_factory=factory,
        metadata_poll_interval_seconds=0.001,
    )

    async def run(lease: KafkaSmokeTopicLease) -> None:
        async with lease:
            assert lease.topic in admin.live_topics

    await asyncio.gather(run(first), run(second))

    assert first.topic != second.topic
    assert admin.deleted_topics == [[first.topic], [second.topic]]
    assert admin.live_topics == {"unowned-topic"}


@pytest.mark.asyncio
async def test_create_failure_does_not_delete_an_unowned_topic() -> None:
    admin = _FakeAdmin(create_succeeds=False, live_topics={"unowned-topic"})
    _factory_calls, factory = _factory_for(admin)
    lease = KafkaSmokeTopicLease(
        bootstrap_servers="broker:9092",
        auth_kwargs={},
        admin_factory=factory,
        metadata_poll_interval_seconds=0.001,
    )

    with pytest.raises(RuntimeError, match="Kafka create failed"):
        async with lease:
            pytest.fail("topic creation should fail")

    assert admin.deleted_topics == []
    assert admin.live_topics == {"unowned-topic"}
    assert admin.closed is True


@pytest.mark.asyncio
async def test_metadata_timeout_cleans_up_created_topic() -> None:
    admin = _FakeAdmin(metadata_ready=False)
    _factory_calls, factory = _factory_for(admin)
    lease = KafkaSmokeTopicLease(
        bootstrap_servers="broker:9092",
        auth_kwargs={},
        admin_factory=factory,
        metadata_timeout_seconds=0.01,
        metadata_poll_interval_seconds=0.001,
    )

    with pytest.raises(TimeoutError, match="metadata did not become ready"):
        async with lease:
            pytest.fail("metadata should time out")

    assert admin.deleted_topics == [[lease.topic]]
    assert admin.live_topics == set()
    assert admin.closed is True
