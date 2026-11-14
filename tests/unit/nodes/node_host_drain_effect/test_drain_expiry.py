# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The drain mark's ttl (OMN-24489): a mark reads draining until its expiry and not draining from it on."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_host_drain_effect.adapters.fakes import FixedLaneSource
from omnimarket.nodes.node_host_drain_effect.adapters.stores import (
    InMemoryDrainStore,
    JsonlDrainStore,
)
from omnimarket.nodes.node_host_drain_effect.handlers.handler_host_drain import (
    HandlerHostDrain,
)
from omnimarket.nodes.node_host_drain_effect.models.model_host_drain import (
    EnumDrainOp,
    EnumDrainOutcome,
    ModelHostDrainRequest,
)
from omnimarket.nodes.node_host_drain_effect.overlay import ModelHostDrainOverlay
from omnimarket.nodes.node_host_drain_effect.protocols import ProtocolDrainStore

NOW = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)
HOST = "example-host-a"
TTL_SECONDS = 3600


class _Clock:
    """A clock a test moves forward by hand."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def _drained(
    overlay: ModelHostDrainOverlay,
    store: ProtocolDrainStore,
    clock: Callable[[], datetime],
    sink: InMemoryEmissionSink,
) -> None:
    """Drain the host for ``TTL_SECONDS`` through the handler, as an operator command would."""
    handler = HandlerHostDrain(
        overlay.drain,
        store=store,
        lanes=FixedLaneSource({HOST: 1}),
        sink=sink,
        clock=clock,
        backend="in_memory",
    )
    result = handler.handle(
        ModelHostDrainRequest(
            op=EnumDrainOp.DRAIN, host_name=HOST, ttl_seconds=TTL_SECONDS
        )
    )
    assert result.outcome is EnumDrainOutcome.DRAINING


def _assert_reads_draining_until_the_ttl(
    read_at: Callable[[int], ProtocolDrainStore], host: str
) -> None:
    """The golden: draining one second before the ttl is up; not draining, and off the list, at it."""
    before = read_at(TTL_SECONDS - 1)
    assert before.is_draining(host) is True
    assert host in before.draining_hosts()
    assert before.until_of(host) is not None
    after = read_at(TTL_SECONDS)
    assert after.is_draining(host) is False
    assert after.draining_hosts() == ()
    assert after.until_of(host) is None


def test_the_in_memory_store_reads_the_mark_expired_at_its_ttl(
    overlay: ModelHostDrainOverlay, sink: InMemoryEmissionSink
) -> None:
    clock = _Clock(NOW)
    store = InMemoryDrainStore(clock=clock)
    _drained(overlay, store, clock, sink)

    def read_at(seconds: int) -> ProtocolDrainStore:
        clock.now = NOW + timedelta(seconds=seconds)
        return store

    _assert_reads_draining_until_the_ttl(read_at, HOST)


def test_the_jsonl_store_reads_the_mark_expired_at_its_ttl(
    overlay: ModelHostDrainOverlay, sink: InMemoryEmissionSink, tmp_path: Path
) -> None:
    path = tmp_path / "drain.jsonl"
    clock = _Clock(NOW)
    _drained(overlay, JsonlDrainStore(path, clock=clock), clock, sink)

    def read_at(seconds: int) -> ProtocolDrainStore:
        return JsonlDrainStore(path, clock=lambda: NOW + timedelta(seconds=seconds))

    _assert_reads_draining_until_the_ttl(read_at, HOST)


def test_without_a_ttl_the_overlay_default_is_used(
    overlay: ModelHostDrainOverlay, sink: InMemoryEmissionSink
) -> None:
    clock = _Clock(NOW)
    store = InMemoryDrainStore(clock=clock)
    handler = HandlerHostDrain(
        overlay.drain,
        store=store,
        lanes=FixedLaneSource({HOST: 0}),
        sink=sink,
        clock=clock,
        backend="in_memory",
    )
    result = handler.handle(ModelHostDrainRequest(op=EnumDrainOp.DRAIN, host_name=HOST))
    assert result.outcome is EnumDrainOutcome.DRAINING
    until = NOW + timedelta(seconds=overlay.drain.default_ttl_seconds)
    assert store.until_of(HOST) == until
    assert result.until == until
    clock.now = until - timedelta(seconds=1)
    assert store.is_draining(HOST) is True
    clock.now = until
    assert store.is_draining(HOST) is False


def test_a_ttl_above_the_overlay_max_fails_before_any_write(
    overlay: ModelHostDrainOverlay, sink: InMemoryEmissionSink
) -> None:
    clock = _Clock(NOW)
    store = InMemoryDrainStore(clock=clock)
    handler = HandlerHostDrain(
        overlay.drain,
        store=store,
        lanes=FixedLaneSource({HOST: 1}),
        sink=sink,
        clock=clock,
        backend="in_memory",
    )
    result = handler.handle(
        ModelHostDrainRequest(
            op=EnumDrainOp.DRAIN,
            host_name=HOST,
            ttl_seconds=overlay.drain.max_ttl_seconds + 1,
        )
    )
    assert result.outcome is EnumDrainOutcome.FAILED
    assert result.failure_code == overlay.drain.codes.ttl_invalid
    # The refusal precedes any write: the store holds no mark.
    assert store.is_draining(HOST) is False
    assert store.draining_hosts() == ()
    assert store.until_of(HOST) is None
    assert [e.phase for e in sink.events] == ["start", "failure"]


class _StoreThatIgnoresExpiry(InMemoryDrainStore):
    """The failing control: reads a draining mark as live no matter how far past its until."""

    def is_draining(self, host_name: str) -> bool:
        record = self._records.get(host_name)
        return record is not None and record[0]


def test_the_failing_control_a_store_that_ignores_expiry_fails_the_golden(
    overlay: ModelHostDrainOverlay, sink: InMemoryEmissionSink
) -> None:
    """A store that never lets a mark expire must make the golden reading go red, not green."""
    clock = _Clock(NOW)
    store = _StoreThatIgnoresExpiry(clock=clock)
    _drained(overlay, store, clock, sink)

    def read_at(seconds: int) -> ProtocolDrainStore:
        clock.now = NOW + timedelta(seconds=seconds)
        return store

    with pytest.raises(AssertionError):
        _assert_reads_draining_until_the_ttl(read_at, HOST)


def test_a_jsonl_record_with_an_until_that_does_not_parse_is_skipped(
    overlay: ModelHostDrainOverlay, tmp_path: Path
) -> None:
    """A bad until is counted and ignored, never read as draining forever."""
    path = tmp_path / "drain.jsonl"
    store = JsonlDrainStore(path, clock=lambda: NOW)
    store.set_draining(
        HOST,
        True,
        reason="rack move",
        at=NOW,
        until=NOW + timedelta(seconds=TTL_SECONDS),
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "host": HOST,
                    "draining": True,
                    "reason": "bad stamp",
                    "at": NOW.isoformat(),
                    "until": "not-a-time",
                }
            )
            + "\n"
        )
    # Past the good mark's ttl: only the bad record could keep the host reading draining.
    reader = JsonlDrainStore(
        path, clock=lambda: NOW + timedelta(seconds=TTL_SECONDS + 1)
    )
    assert reader.is_draining(HOST) is False
    assert reader.skipped_lines == 1
