# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3 replay falsifiers: incomplete broker windows and false key parity.

These cases encode the failure modes in the approved OMN-15359 amendment before
the verifier exists. A green empty replay, same-count key substitution, lost
partition, unreadable record or truncated read would all misstate AC3.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from aiokafka import TopicPartition as _Partition

from omnimarket.projection.ac3_replay import (
    ReplayError,
    capture_retained,
    compare_tenant_keys,
)


@dataclass(frozen=True)
class _Message:
    topic: str
    partition: int
    offset: int
    value: bytes


class _BrokerFixture:
    def __init__(
        self,
        records: dict[_Partition, list[_Message]],
        *,
        assigned: list[_Partition] | None = None,
    ) -> None:
        self.records = records
        self.assigned = assigned if assigned is not None else list(records)
        self._assignment_override = assigned is not None
        self.positions: dict[_Partition, int] = {}
        self.options: dict[str, object] = {}
        self.stopped = False

    def factory(self, *topics: str, **options: object) -> _BrokerFixture:
        assert not topics
        self.options = options
        return self

    async def start(self) -> None:
        return None

    def assignment(self) -> set[_Partition]:
        return set(self.assigned)

    def partitions_for_topic(self, topic: str) -> set[int]:
        return {tp.partition for tp in self.records if tp.topic == topic}

    def assign(self, partitions: list[_Partition]) -> None:
        if not self._assignment_override:
            self.assigned = partitions

    async def beginning_offsets(self, tps: list[_Partition]) -> dict[_Partition, int]:
        return {tp: min((r.offset for r in self.records[tp]), default=0) for tp in tps}

    async def end_offsets(self, tps: list[_Partition]) -> dict[_Partition, int]:
        return {
            tp: max((r.offset for r in self.records[tp]), default=-1) + 1 for tp in tps
        }

    def seek(self, tp: _Partition, offset: int) -> None:
        self.positions[tp] = offset

    async def position(self, tp: _Partition) -> int:
        return self.positions[tp]

    async def getmany(
        self, *tps: _Partition, timeout_ms: int
    ) -> dict[_Partition, list[_Message]]:
        del timeout_ms
        batch: dict[_Partition, list[_Message]] = {}
        for tp in tps:
            remaining = [r for r in self.records[tp] if r.offset >= self.positions[tp]]
            if remaining:
                batch[tp] = remaining[:1]
                self.positions[tp] = remaining[0].offset + 1
        return batch

    async def stop(self) -> None:
        self.stopped = True


def _wire(tenant: str, correlation: str) -> bytes:
    return json.dumps(
        {
            "tenant_id": tenant,
            "payload": {"tenant_id": tenant, "correlation_id": correlation},
        }
    ).encode()


def _rows(tenant: str, *correlations: str) -> list[dict[str, str]]:
    return [
        {"tenant_id": tenant, "correlation_id": correlation}
        for correlation in correlations
    ]


def test_two_tenants_have_exact_nonempty_key_parity() -> None:
    expected = {"tenant-a": {"run-a1", "run-a2"}, "tenant-b": {"run-b1"}}
    live = _rows("tenant-a", "run-a1", "run-a2") + _rows("tenant-b", "run-b1")
    comparison = compare_tenant_keys(expected, live_rows=live, replay_rows=live)
    assert set(comparison) == set(expected)
    assert comparison["tenant-a"].live_count == 2
    assert comparison["tenant-b"].replay_count == 1
    assert (
        comparison["tenant-a"].live_key_sha256
        == comparison["tenant-a"].replay_key_sha256
    )


@pytest.mark.parametrize(
    ("live", "replay", "reason"),
    [
        (_rows("tenant-a", "run-a1"), [], "key mismatch"),
        (_rows("tenant-a", "run-a1"), _rows("tenant-a", "run-other"), "key mismatch"),
        (_rows("tenant-a", "run-a1"), _rows("tenant-b", "run-a1"), "key mismatch"),
        (
            _rows("tenant-a", "run-a1"),
            _rows("tenant-a", "run-a1", "run-a1"),
            "duplicate",
        ),
    ],
)
def test_false_parity_refused(
    live: list[dict[str, str]], replay: list[dict[str, str]], reason: str
) -> None:
    other = _rows("tenant-b", "run-b1")
    with pytest.raises(ReplayError, match=reason):
        compare_tenant_keys(
            {"tenant-a": {"run-a1"}, "tenant-b": {"run-b1"}},
            live_rows=live + other,
            replay_rows=replay + other,
        )


def test_two_empty_tenants_cannot_prove_parity() -> None:
    with pytest.raises(ReplayError, match="key mismatch"):
        compare_tenant_keys(
            {"tenant-a": {"run-a1"}, "tenant-b": {"run-b1"}},
            live_rows=[],
            replay_rows=[],
        )


@pytest.mark.asyncio
async def test_retained_offsets_are_fixed_and_consumer_never_commits() -> None:
    topic = "onex.evt.omnibase-infra.delegation-completed.v1"
    first, second = _Partition(topic, 0), _Partition(topic, 1)
    fixture = _BrokerFixture(
        {
            first: [_Message(topic, 0, 4, _wire("tenant-a", "run-a1"))],
            second: [_Message(topic, 1, 9, _wire("tenant-b", "run-b1"))],
        }
    )
    records, spans = await capture_retained(fixture.factory, (topic,), max_idle_polls=2)
    assert len(records) == 2
    assert {(span.partition, span.start, span.end, span.final) for span in spans} == {
        (0, 4, 5, 5),
        (1, 9, 10, 10),
    }
    assert fixture.options["group_id"] is None
    assert fixture.options["enable_auto_commit"] is False
    assert fixture.stopped


@pytest.mark.asyncio
async def test_missing_partition_is_a_failure() -> None:
    topic = "onex.evt.omnibase-infra.delegation-completed.v1"
    tp = _Partition(topic, 0)
    fixture = _BrokerFixture(
        {tp: [_Message(topic, 0, 0, _wire("a", "x"))]}, assigned=[]
    )
    with pytest.raises(ReplayError):
        await capture_retained(fixture.factory, (topic,), max_idle_polls=1)
    assert fixture.stopped


@pytest.mark.asyncio
async def test_decode_error_is_a_failure() -> None:
    topic = "onex.evt.omnibase-infra.delegation-completed.v1"
    tp = _Partition(topic, 0)
    fixture = _BrokerFixture({tp: [_Message(topic, 0, 0, b"{broken")]})
    with pytest.raises(ReplayError):
        await capture_retained(fixture.factory, (topic,), max_idle_polls=1)


@pytest.mark.asyncio
async def test_incomplete_read_is_a_failure() -> None:
    topic = "onex.evt.omnibase-infra.delegation-completed.v1"
    tp = _Partition(topic, 0)
    fixture = _BrokerFixture({tp: [_Message(topic, 0, 0, _wire("a", "x"))]})
    fixture.getmany = _empty_batch  # type: ignore[method-assign]
    with pytest.raises(ReplayError):
        await capture_retained(fixture.factory, (topic,), max_idle_polls=1)


async def _empty_batch(
    *tps: _Partition, timeout_ms: int
) -> dict[_Partition, list[_Message]]:
    del tps, timeout_ms
    return {}
