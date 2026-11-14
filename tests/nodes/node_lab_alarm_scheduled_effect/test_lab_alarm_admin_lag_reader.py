# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime_admin lag reader: committed against end offsets, the admin client only (OMN-24288).

The reader is exercised against a fake admin injected through ``admin_factory``: it answers the
two read-only calls the reader makes (``list_consumer_group_offsets``, then ``list_offsets``)
with the future shape the real client returns, and records every attribute touched so the test
can name exactly the two. A broker failure must be classified by its exception type only, never
its text, which carries the broker's address. The overlay tests pin the ``runtime_admin``
contract: ``bootstrap_servers`` belongs to that backend and is required by it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from confluent_kafka import OFFSET_INVALID, TopicPartition
from pydantic import ValidationError

from omnimarket.nodes.node_lab_alarm.adapters.runtime_adapters import AdminLag
from omnimarket.nodes.node_lab_alarm.overlay import ModelAlarmBackend
from omnimarket.nodes.node_lab_alarm.protocols import (
    GroupNeverCommittedError,
    GroupUnreadableError,
)

pytestmark = pytest.mark.unit

_GROUP = (
    "local.examplepkg.projection_example.consume.1.2.0.__i.runtime-main.__t.orders.v1"
)
_BOOTSTRAP = "broker.example.invalid:9092"
_TIMEOUT = 0.5


class _Future:
    """The future shape the reader waits on: ``result(timeout=...)`` returns the payload."""

    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.timeouts: list[float | None] = []

    def result(self, timeout: float | None = None) -> Any:
        self.timeouts.append(timeout)
        return self._payload


class _Committed:
    """The payload of the committed-offsets answer: its ``topic_partitions`` list."""

    def __init__(self, topic_partitions: list[Any]) -> None:
        self.topic_partitions = topic_partitions


class _EndOffset:
    """The payload of one end-offset answer: the offset alone."""

    def __init__(self, offset: int) -> None:
        self.offset = offset


class _FakeAdmin:
    """The read-only admin client: the two calls the reader may make, and nothing else.

    Every attribute the reader touches lands in ``used``, so a call outside the two read-only
    ones fails the assertion that names exactly them. ``boom`` makes the committed-offsets
    call raise, the way a broker that refuses the read does.
    """

    def __init__(
        self, group: str, committed: list[Any], ends: Mapping[tuple[str, int], int]
    ) -> None:
        self.group = group
        self.committed = committed
        self.ends = dict(ends)
        self.config: dict[str, str] | None = None
        self.used: set[str] = set()
        self.boom: Exception | None = None

    def list_consumer_group_offsets(
        self, requests: object, request_timeout: float | None = None
    ) -> dict[str, _Future]:
        self.used.add("list_consumer_group_offsets")
        if self.boom is not None:
            raise self.boom
        return {self.group: _Future(_Committed(list(self.committed)))}

    def list_offsets(
        self, request: Mapping[Any, Any], request_timeout: float | None = None
    ) -> dict[Any, _Future]:
        self.used.add("list_offsets")
        return {
            tp: _Future(_EndOffset(self.ends[(tp.topic, tp.partition)]))
            for tp in request
        }

    def __getattr__(self, name: str) -> Any:
        if not name.startswith("_"):
            self.used.add(name)
        raise AttributeError(name)


def _reader(fake: _FakeAdmin) -> AdminLag:
    def factory(config: dict[str, str]) -> _FakeAdmin:
        fake.config = config
        return fake

    return AdminLag(_BOOTSTRAP, _TIMEOUT, admin_factory=factory)


def test_the_lag_is_the_sum_of_end_minus_committed_over_partitions() -> None:
    fake = _FakeAdmin(
        _GROUP,
        [TopicPartition("orders", 0, 5), TopicPartition("orders", 1, 3)],
        {("orders", 0): 9, ("orders", 1): 3},
    )

    assert _reader(fake).total_lag(_GROUP) == 4


def test_the_lag_counts_only_partitions_that_hold_a_committed_offset() -> None:
    # OFFSET_INVALID is how the broker reports a partition the group holds no offset on: the
    # reader must not count it and must not ask for its end offset (the fake has none to give).
    fake = _FakeAdmin(
        _GROUP,
        [TopicPartition("orders", 0, 5), TopicPartition("orders", 1, OFFSET_INVALID)],
        {("orders", 0): 9},
    )

    assert _reader(fake).total_lag(_GROUP) == 4


def test_a_group_that_committed_nowhere_raises_group_never_committed() -> None:
    fake = _FakeAdmin(_GROUP, [], {})

    with pytest.raises(GroupNeverCommittedError, match="no committed offset"):
        _reader(fake).total_lag(_GROUP)
    assert fake.used == {"list_consumer_group_offsets"}


def test_a_broker_failure_is_classified_by_type_and_never_its_text() -> None:
    fake = _FakeAdmin(_GROUP, [], {})
    fake.boom = RuntimeError("secret-broker-address")

    with pytest.raises(GroupUnreadableError) as raised:
        _reader(fake).total_lag(_GROUP)
    assert "RuntimeError" in str(raised.value)
    assert "secret-broker-address" not in str(raised.value)


def test_the_reader_touches_only_the_two_read_only_calls_on_the_declared_broker() -> (
    None
):
    fake = _FakeAdmin(
        _GROUP,
        [TopicPartition("orders", 0, 5), TopicPartition("orders", 1, 3)],
        {("orders", 0): 9, ("orders", 1): 3},
    )

    _reader(fake).total_lag(_GROUP)

    assert fake.used == {"list_consumer_group_offsets", "list_offsets"}
    assert fake.config == {"bootstrap.servers": _BOOTSTRAP}


def test_runtime_admin_without_bootstrap_servers_is_refused() -> None:
    with pytest.raises(ValidationError, match="bootstrap_servers"):
        ModelAlarmBackend(backend="runtime_admin", lane="dev")


def test_another_backend_with_bootstrap_servers_is_refused() -> None:
    with pytest.raises(ValidationError, match="bootstrap_servers"):
        ModelAlarmBackend(backend="runtime", lane="dev", bootstrap_servers="x:1")


def test_runtime_admin_with_bootstrap_servers_loads() -> None:
    backend = ModelAlarmBackend(
        backend="runtime_admin", lane="dev", bootstrap_servers=_BOOTSTRAP
    )
    assert backend.bootstrap_servers == _BOOTSTRAP
