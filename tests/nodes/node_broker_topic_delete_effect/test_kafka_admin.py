# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Kafka admin adapter of the broker topic delete (OMN-23879), against hand-written fake clients.

No network: the fakes answer the admin and consumer calls the adapter makes, so the evidence mapping,
the typed failures and the delete outcomes are read from the adapter's own code.
"""

from __future__ import annotations

from concurrent.futures import Future
from types import SimpleNamespace
from typing import Any

import pytest

from omnimarket.nodes.node_broker_topic_delete_effect.adapters.kafka_admin import (
    REQUIRED_ACL_OPERATIONS,
    KafkaBrokerTopicAdmin,
)
from omnimarket.nodes.node_broker_topic_delete_effect.protocols import BrokerAdminError
from tests.nodes.node_broker_topic_delete_effect.delete_support import overlay

pytestmark = pytest.mark.unit


def _done(value: Any) -> Future[Any]:
    future: Future[Any] = Future()
    future.set_result(value)
    return future


def _failed(exc: Exception) -> Future[Any]:
    future: Future[Any] = Future()
    future.set_exception(exc)
    return future


class FakeAdmin:
    """Topics with partition ids, groups with members and committed offsets, and recorded deletes."""

    def __init__(
        self,
        *,
        topics: dict[str, int],
        members: dict[str, str],
        committed: dict[str, str],
        listing_errors: tuple[str, ...] = (),
        delete_failures: dict[str, Exception] | None = None,
    ) -> None:
        self._topics = topics
        self._members = members  # topic -> group with a live member
        self._committed = committed  # topic -> group with a committed offset
        self._listing_errors = listing_errors
        self._delete_failures = delete_failures or {}
        self.deleted: list[str] = []

    def list_topics(self, timeout: float) -> Any:
        return SimpleNamespace(
            topics={
                name: SimpleNamespace(
                    partitions=dict.fromkeys(range(count)), error=None
                )
                for name, count in self._topics.items()
            }
        )

    def list_consumer_groups(self, request_timeout: float) -> Future[Any]:
        groups = sorted({*self._members.values(), *self._committed.values()})
        return _done(
            SimpleNamespace(
                valid=[SimpleNamespace(group_id=group) for group in groups],
                errors=list(self._listing_errors),
            )
        )

    def describe_consumer_groups(
        self, group_ids: list[str], request_timeout: float
    ) -> dict[str, Future[Any]]:
        out: dict[str, Future[Any]] = {}
        for group in group_ids:
            topics = [t for t, g in self._members.items() if g == group]
            members = [
                SimpleNamespace(
                    assignment=SimpleNamespace(
                        topic_partitions=[SimpleNamespace(topic=t) for t in topics]
                    )
                )
            ]
            out[group] = _done(SimpleNamespace(members=members if topics else []))
        return out

    def list_consumer_group_offsets(
        self, requests: list[Any], request_timeout: float
    ) -> dict[str, Future[Any]]:
        group = requests[0].group_id
        topics = [t for t, g in self._committed.items() if g == group]
        return {
            group: _done(
                SimpleNamespace(
                    topic_partitions=[SimpleNamespace(topic=t) for t in topics]
                )
            )
        }

    def delete_topics(
        self, names: list[str], request_timeout: float
    ) -> dict[str, Future[Any]]:
        self.deleted.extend(names)
        return {
            name: _failed(self._delete_failures[name])
            if name in self._delete_failures
            else _done(None)
            for name in names
        }


class FakeConsumer:
    """Watermarks per topic as (low, high) and one retained record's timestamp."""

    def __init__(self, marks: dict[str, tuple[int, int]], stamp_ms: int) -> None:
        self._marks = marks
        self._stamp = stamp_ms
        self._assigned: Any = None
        self.closed = False

    def get_watermark_offsets(
        self, partition: Any, timeout: float, cached: bool
    ) -> tuple[int, int]:
        return self._marks[partition.topic]

    def assign(self, partitions: list[Any]) -> None:
        self._assigned = partitions

    def poll(self, timeout: float) -> Any:
        return SimpleNamespace(error=lambda: None, timestamp=lambda: (1, self._stamp))

    def close(self) -> None:
        self.closed = True


def _adapter(
    admin: FakeAdmin, consumer: FakeConsumer, *, resolver: Any = None
) -> KafkaBrokerTopicAdmin:
    return KafkaBrokerTopicAdmin(
        overlay(),
        resolver=resolver or (lambda lane: {"bootstrap.servers": lane}),
        admin_factory=lambda _config: admin,
        consumer_factory=lambda _config: consumer,
    )


def _world() -> tuple[FakeAdmin, FakeConsumer]:
    admin = FakeAdmin(
        topics={"empty": 1, "grouped": 2, "offsets": 1, "records": 1},
        members={"grouped": "g-live"},
        committed={"offsets": "g-offsets"},
    )
    consumer = FakeConsumer(
        {
            "empty": (0, 0),
            "grouped": (0, 0),
            "offsets": (0, 0),
            "records": (2, 7),
        },
        stamp_ms=1_700_000_000_000,
    )
    return admin, consumer


def test_evidence_maps_each_topic_from_the_brokers_reads() -> None:
    admin, consumer = _world()
    evidence = _adapter(admin, consumer).read_evidence(
        ["empty", "grouped", "offsets", "records", "missing"]
    )
    assert evidence["empty"].exists is True
    assert evidence["empty"].high_watermark_total == 0
    assert evidence["empty"].last_record_ms is None
    assert evidence["grouped"].consumer_groups == ("g-live",)
    assert evidence["grouped"].partitions == 2
    assert evidence["offsets"].offset_groups == ("g-offsets",)
    assert evidence["offsets"].consumer_groups == ()
    assert evidence["records"].high_watermark_total == 7
    assert evidence["records"].retained_records == 5
    assert evidence["records"].last_record_ms == 1_700_000_000_000
    assert evidence["missing"].exists is False
    assert consumer.closed is True


def test_an_incomplete_group_listing_is_a_typed_failure() -> None:
    admin, consumer = _world()
    admin._listing_errors = ("boom",)
    with pytest.raises(BrokerAdminError, match="group_listing_incomplete"):
        _adapter(admin, consumer).read_evidence(["empty"])


def test_delete_returns_the_broker_outcome_per_name_and_sends_only_the_given_names() -> (
    None
):
    admin, consumer = _world()
    admin._delete_failures = {"grouped": RuntimeError("TOPIC_AUTHORIZATION_FAILED")}
    outcome = _adapter(admin, consumer).delete(["empty", "grouped"])
    assert admin.deleted == ["empty", "grouped"]
    assert outcome["empty"] == ""
    assert outcome["grouped"] == "RuntimeError"


def test_an_unresolvable_credential_is_a_typed_failure_on_first_use() -> None:
    admin, consumer = _world()

    def refuse(lane: str) -> dict[str, str]:
        raise ValueError("lane declares no credential")

    adapter = _adapter(admin, consumer, resolver=refuse)
    with pytest.raises(
        BrokerAdminError, match="broker_credential_unresolved:ValueError"
    ):
        adapter.read_evidence(["empty"])


def test_the_acl_operations_the_adapter_needs_are_named() -> None:
    assert REQUIRED_ACL_OPERATIONS == (
        ("CLUSTER", "DESCRIBE"),
        ("GROUP", "DESCRIBE"),
        ("TOPIC", "DESCRIBE"),
        ("TOPIC", "READ"),
        ("TOPIC", "DELETE"),
    )
