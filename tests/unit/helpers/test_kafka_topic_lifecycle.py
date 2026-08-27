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
    cleanup_kafka_smoke_consumer,
    consumer_group_names,
    topic_scoped_consumer_group_id,
    wait_for_consumer_group_absence,
)


@dataclass
class _FakeAdmin:
    """Typed admin double that models create, metadata, and delete calls."""

    create_succeeds: bool = True
    metadata_ready: bool = True
    live_topics: set[str] = field(default_factory=set)
    live_groups: set[str] = field(default_factory=set)
    created_topics: list[list[str]] = field(default_factory=list)
    deleted_topics: list[list[str]] = field(default_factory=list)
    deleted_groups: list[list[str]] = field(default_factory=list)
    describe_calls: list[list[str]] = field(default_factory=list)
    group_list_calls: int = 0
    create_response: object | None = None
    delete_response: object | None = None
    delete_group_response: object | None = None
    delete_topic_error: BaseException | None = None
    delete_group_error: BaseException | None = None
    close_error: BaseException | None = None
    started: bool = False
    closed: bool = False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error

    async def create_topics(self, topics: list[NewTopic]) -> object:
        names = [topic.name for topic in topics]
        self.created_topics.append(names)
        if self.create_response is not None:
            return self.create_response
        if not self.create_succeeds:
            return SimpleNamespace(topic_errors=[(names[0], 36, "already exists")])
        self.live_topics.update(names)
        return SimpleNamespace(topic_errors=[(name, 0, "") for name in names])

    async def delete_topics(self, topics: list[str]) -> object:
        self.deleted_topics.append(list(topics))
        self.live_topics.difference_update(topics)
        if self.delete_topic_error is not None:
            raise self.delete_topic_error
        if self.delete_response is not None:
            return self.delete_response
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

    async def list_consumer_groups(self) -> object:
        self.group_list_calls += 1
        return [(group_id, "consumer") for group_id in sorted(self.live_groups)]

    async def delete_consumer_groups(self, groups: list[str]) -> object:
        self.deleted_groups.append(list(groups))
        if self.delete_group_error is not None:
            raise self.delete_group_error
        if self.delete_group_response is not None:
            return self.delete_group_response
        self.live_groups.difference_update(groups)
        return SimpleNamespace(results=[(group_id, 0) for group_id in groups])


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


def test_effective_group_binding_rejects_another_lease_or_unscoped_group() -> None:
    lease = KafkaSmokeTopicLease(
        bootstrap_servers="broker:9092",
        auth_kwargs={},
        admin_factory=_factory_for(_FakeAdmin())[1],
    )

    with pytest.raises(ValueError, match="topic-scoped"):
        lease.bind_effective_group_id("unowned-group")

    effective_group_id = topic_scoped_consumer_group_id(
        group_id=lease.group_id,
        topic=lease.topic,
    )
    lease.bind_effective_group_id(effective_group_id)
    with pytest.raises(ValueError, match="already bound"):
        lease.bind_effective_group_id(f"{lease.group_id}-other.__t.{lease.topic}")


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


def test_consumer_group_census_is_value_safe_and_exact() -> None:
    assert consumer_group_names(
        SimpleNamespace(groups=[("owned-group", "consumer"), ("other", "")])
    ) == ("owned-group", "other")
    assert consumer_group_names({"groups": [{"group_id": "mapping-group"}]}) == (
        "mapping-group",
    )


@pytest.mark.asyncio
async def test_wait_for_consumer_group_absence_proves_census_absence() -> None:
    admin = _FakeAdmin(live_groups={"unowned-group"})

    assert await wait_for_consumer_group_absence(
        admin,
        "owned-group",
        timeout_seconds=0.01,
        poll_interval_seconds=0.001,
    )
    assert admin.group_list_calls == 1


@pytest.mark.asyncio
async def test_lease_deletes_only_its_bound_effective_group() -> None:
    admin = _FakeAdmin(live_groups={"unowned-group"})
    _factory_calls, factory = _factory_for(admin)
    lease = KafkaSmokeTopicLease(
        bootstrap_servers="broker:9092",
        auth_kwargs={},
        admin_factory=factory,
        metadata_poll_interval_seconds=0.001,
    )
    effective_group_id = topic_scoped_consumer_group_id(
        group_id=lease.group_id,
        topic=lease.topic,
    )
    lease.bind_effective_group_id(effective_group_id)
    admin.live_groups.add(effective_group_id)

    async with lease:
        assert lease.effective_group_id == effective_group_id

    assert admin.deleted_groups == [[effective_group_id]]
    assert effective_group_id not in admin.live_groups
    assert "unowned-group" in admin.live_groups
    assert lease.effective_group_id is None


@pytest.mark.asyncio
async def test_bound_group_already_absent_is_verified_without_delete() -> None:
    admin = _FakeAdmin()
    _factory_calls, factory = _factory_for(admin)
    lease = KafkaSmokeTopicLease(
        bootstrap_servers="broker:9092",
        auth_kwargs={},
        admin_factory=factory,
        metadata_poll_interval_seconds=0.001,
    )
    group_id = topic_scoped_consumer_group_id(
        group_id=lease.group_id,
        topic=lease.topic,
    )
    lease.bind_effective_group_id(group_id)

    async with lease:
        pass

    assert admin.deleted_groups == []
    assert lease.effective_group_id is None


@pytest.mark.asyncio
async def test_parallel_leases_keep_group_cleanup_disjoint() -> None:
    admin = _FakeAdmin()
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
    first_group = topic_scoped_consumer_group_id(
        group_id=first.group_id,
        topic=first.topic,
    )
    second_group = topic_scoped_consumer_group_id(
        group_id=second.group_id,
        topic=second.topic,
    )
    first.bind_effective_group_id(first_group)
    second.bind_effective_group_id(second_group)
    admin.live_groups.update({first_group, second_group})

    async def run(lease: KafkaSmokeTopicLease) -> None:
        async with lease:
            pass

    await asyncio.gather(run(first), run(second))

    assert admin.deleted_groups == [[first_group], [second_group]]
    assert admin.live_groups == set()


@pytest.mark.asyncio
async def test_create_response_rejects_empty_result() -> None:
    admin = _FakeAdmin(create_response=SimpleNamespace(topic_errors=[]))
    _factory_calls, factory = _factory_for(admin)
    lease = KafkaSmokeTopicLease(
        bootstrap_servers="broker:9092",
        auth_kwargs={},
        admin_factory=factory,
        metadata_poll_interval_seconds=0.001,
    )

    with pytest.raises(RuntimeError, match="no topic result"):
        async with lease:
            pass

    assert admin.deleted_topics == []
    assert admin.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["mismatch", "duplicate"])
async def test_create_response_requires_one_exact_topic(shape: str) -> None:
    admin = _FakeAdmin()
    _factory_calls, factory = _factory_for(admin)
    lease = KafkaSmokeTopicLease(
        bootstrap_servers="broker:9092",
        auth_kwargs={},
        admin_factory=factory,
        metadata_poll_interval_seconds=0.001,
    )
    if shape == "mismatch":
        entries = [("not-owned", 0, "")]
        match = "unexpected topic"
    else:
        entries = [(lease.topic, 0, ""), (lease.topic, 0, "")]
        match = "duplicate topic"
    admin.create_response = SimpleNamespace(topic_errors=entries)

    with pytest.raises(RuntimeError, match=match):
        async with lease:
            pass

    assert admin.deleted_topics == []
    assert admin.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "match"),
    [
        (SimpleNamespace(topic_error_codes=[("not-owned", 0)]), "unexpected topic"),
        (
            SimpleNamespace(topic_error_codes=[]),
            "no topic result",
        ),
        (
            SimpleNamespace(topic_error_codes=[("expected", 0), ("expected", 0)]),
            "duplicate topic",
        ),
    ],
)
async def test_delete_response_requires_one_exact_success(
    response: object,
    match: str,
) -> None:
    admin = _FakeAdmin()
    _factory_calls, factory = _factory_for(admin)
    lease = KafkaSmokeTopicLease(
        bootstrap_servers="broker:9092",
        auth_kwargs={},
        admin_factory=factory,
        metadata_poll_interval_seconds=0.001,
    )
    # Response names are corrected to the actual allocated topic so this test
    # exercises the shape error (empty/mismatch/duplicate), not a fixed name.
    if match != "unexpected topic":
        entries = [(lease.topic, 0)] * 2 if match == "duplicate topic" else []
        response = SimpleNamespace(topic_error_codes=entries)
    else:
        response = SimpleNamespace(topic_error_codes=[("not-owned", 0)])
    admin.delete_response = response

    with pytest.raises(RuntimeError, match=match):
        async with lease:
            pass

    assert admin.deleted_topics == [[lease.topic]]
    assert admin.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "match"),
    [
        (SimpleNamespace(results=[]), "no result"),
        (SimpleNamespace(results=[("not-owned", 0)]), "unexpected group"),
        (
            SimpleNamespace(results=[("expected", 0), ("expected", 0)]),
            "duplicate group",
        ),
    ],
)
async def test_delete_group_response_requires_one_exact_success(
    response: object,
    match: str,
) -> None:
    admin = _FakeAdmin()
    _factory_calls, factory = _factory_for(admin)
    lease = KafkaSmokeTopicLease(
        bootstrap_servers="broker:9092",
        auth_kwargs={},
        admin_factory=factory,
        metadata_poll_interval_seconds=0.001,
    )
    group_id = topic_scoped_consumer_group_id(
        group_id=lease.group_id,
        topic=lease.topic,
    )
    lease.bind_effective_group_id(group_id)
    admin.live_groups.add(group_id)
    if match != "unexpected group":
        entries = [(group_id, 0)] * 2 if match == "duplicate group" else []
        response = SimpleNamespace(results=entries)
    admin.delete_group_response = response

    with pytest.raises(RuntimeError, match=match):
        async with lease:
            pass

    assert admin.deleted_groups == [[group_id]]
    assert admin.deleted_topics == [[lease.topic]]
    assert admin.closed is True


@pytest.mark.asyncio
async def test_cleanup_preserves_primary_and_runs_all_lease_phases() -> None:
    admin = _FakeAdmin(
        delete_topic_error=RuntimeError("topic cleanup boom"),
        delete_group_error=RuntimeError("group cleanup boom"),
    )
    _factory_calls, factory = _factory_for(admin)
    lease = KafkaSmokeTopicLease(
        bootstrap_servers="broker:9092",
        auth_kwargs={},
        admin_factory=factory,
        metadata_poll_interval_seconds=0.001,
    )
    group_id = topic_scoped_consumer_group_id(
        group_id=lease.group_id,
        topic=lease.topic,
    )
    lease.bind_effective_group_id(group_id)
    admin.live_groups.add(group_id)
    primary = ValueError("primary test failure")

    with pytest.raises(ValueError, match="primary test failure") as caught:
        async with lease:
            raise primary

    assert caught.value is primary
    assert primary.__notes__ == [
        "Kafka smoke cleanup failed during consumer-group: RuntimeError: group cleanup boom",
        "Kafka smoke cleanup failed during topic: RuntimeError: topic cleanup boom",
    ]
    assert admin.deleted_groups == [[group_id]]
    assert admin.deleted_topics == [[lease.topic]]
    assert admin.closed is True


@pytest.mark.asyncio
async def test_cleanup_without_primary_raises_and_attempts_every_phase() -> None:
    admin = _FakeAdmin(
        delete_topic_error=RuntimeError("topic cleanup boom"),
        delete_group_error=RuntimeError("group cleanup boom"),
    )
    _factory_calls, factory = _factory_for(admin)
    lease = KafkaSmokeTopicLease(
        bootstrap_servers="broker:9092",
        auth_kwargs={},
        admin_factory=factory,
        metadata_poll_interval_seconds=0.001,
    )
    group_id = topic_scoped_consumer_group_id(
        group_id=lease.group_id,
        topic=lease.topic,
    )
    lease.bind_effective_group_id(group_id)
    admin.live_groups.add(group_id)

    with pytest.raises(RuntimeError, match="group cleanup boom") as caught:
        async with lease:
            pass

    assert (
        "Kafka smoke cleanup failed during topic: RuntimeError: topic cleanup boom"
        in (caught.value.__notes__)
    )
    assert admin.deleted_groups == [[group_id]]
    assert admin.deleted_topics == [[lease.topic]]
    assert admin.closed is True


async def _cancel_raising_consumer(cancel_seen: asyncio.Event) -> None:
    try:
        await asyncio.sleep(10)
    except asyncio.CancelledError:
        cancel_seen.set()
        raise RuntimeError("consumer task cleanup boom") from None


@pytest.mark.asyncio
async def test_consumer_cleanup_preserves_primary_and_cancels_after_unsubscribe_error() -> (
    None
):
    cancel_seen = asyncio.Event()
    consuming_task = asyncio.create_task(_cancel_raising_consumer(cancel_seen))
    await asyncio.sleep(0)

    async def unsubscribe() -> None:
        raise RuntimeError("unsubscribe cleanup boom")

    primary = ValueError("primary test failure")
    await cleanup_kafka_smoke_consumer(
        unsubscribe,
        consuming_task,
        primary_error=primary,
    )

    assert cancel_seen.is_set()
    assert primary.__notes__ == [
        "Kafka smoke cleanup failed during unsubscribe: RuntimeError: unsubscribe cleanup boom",
        "Kafka smoke cleanup failed during consumer-task: RuntimeError: consumer task cleanup boom",
    ]


@pytest.mark.asyncio
async def test_consumer_cleanup_without_primary_raises_after_unsubscribe_and_cancel() -> (
    None
):
    cancel_seen = asyncio.Event()
    consuming_task = asyncio.create_task(_cancel_raising_consumer(cancel_seen))
    await asyncio.sleep(0)

    async def unsubscribe() -> None:
        raise RuntimeError("unsubscribe cleanup boom")

    with pytest.raises(RuntimeError, match="unsubscribe cleanup boom") as caught:
        await cleanup_kafka_smoke_consumer(
            unsubscribe,
            consuming_task,
            primary_error=None,
        )

    assert cancel_seen.is_set()
    assert (
        "Kafka smoke cleanup failed during consumer-task: RuntimeError: consumer task cleanup boom"
        in (caught.value.__notes__)
    )
