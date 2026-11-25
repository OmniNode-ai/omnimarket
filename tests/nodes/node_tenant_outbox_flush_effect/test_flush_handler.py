# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The outbox flush: a published row is not sent twice, a failed publish leaves the row, a mark follows only an ack."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_tenant_outbox_flush_effect.handlers.handler_tenant_outbox_flush import (
    HandlerTenantOutboxFlush,
)
from omnimarket.nodes.node_tenant_outbox_flush_effect.models.model_tenant_outbox_flush import (
    EnumTenantOutboxFlushOutcome,
    ModelOutboxRow,
    ModelTenantOutboxFlushCommand,
)

pytestmark = pytest.mark.unit

TOPIC = "test.tenant.events"
NOW = datetime(2026, 11, 25, 6, 0, tzinfo=UTC)


def _envelope(slug: str) -> dict[str, Any]:
    tenant_id = str(uuid4())
    return {
        "operation": "TENANT_CREATED",
        "correlation_id": str(uuid4()),
        "payload": {"tenant": {"tenant_id": tenant_id, "tenant_slug": slug}},
    }


class FakeStore:
    def __init__(self, rows: Sequence[ModelOutboxRow]) -> None:
        self.rows = {r.row_id: r for r in rows}
        self.published: dict[str, datetime] = {}
        self.failures: dict[str, str] = {}
        self.fail_reads = False
        self.fail_marks = False

    async def fetch_unpublished(self, limit: int) -> Sequence[ModelOutboxRow]:
        if self.fail_reads:
            raise OSError("down")
        pending = [r for r in self.rows.values() if r.row_id not in self.published]
        return pending[:limit]

    async def mark_published(self, row_id: str, published_at: datetime) -> None:
        if self.fail_marks:
            raise OSError("down")
        self.published[row_id] = published_at

    async def record_failure(self, row_id: str, error: str) -> None:
        self.failures[row_id] = error


class FakeBus:
    def __init__(self, refuse: set[str] | None = None) -> None:
        self.sent: list[tuple[str, str, Mapping[str, Any]]] = []
        self.refuse = refuse or set()

    async def publish(self, topic: str, key: str, value: Mapping[str, Any]) -> None:
        if value["payload"]["tenant"]["tenant_slug"] in self.refuse:
            raise TimeoutError("no broker ack")
        self.sent.append((topic, key, value))


def _rows(*slugs: str) -> list[ModelOutboxRow]:
    return [
        ModelOutboxRow(row_id=str(i), envelope=_envelope(s), attempts=0)
        for i, s in enumerate(slugs, start=1)
    ]


def _handler(store: FakeStore, bus: FakeBus) -> HandlerTenantOutboxFlush:
    return HandlerTenantOutboxFlush(store, bus, topic=TOPIC, clock=lambda: NOW)


def _cmd(limit: int = 100) -> ModelTenantOutboxFlushCommand:
    return ModelTenantOutboxFlushCommand(command_id=uuid4(), batch_limit=limit)


def test_publishes_each_row_then_marks_it() -> None:
    store, bus = FakeStore(_rows("alpha", "bravo")), FakeBus()
    result = asyncio.run(_handler(store, bus).handle(_cmd()))
    assert result.outcome is EnumTenantOutboxFlushOutcome.FLUSHED
    assert result.published == 2
    assert result.failed == 0
    assert [t for t, _, _ in bus.sent] == [TOPIC, TOPIC]
    assert set(store.published) == {"1", "2"}


def test_a_published_row_is_not_sent_twice() -> None:
    store, bus = FakeStore(_rows("alpha")), FakeBus()
    handler = _handler(store, bus)
    asyncio.run(handler.handle(_cmd()))
    second = asyncio.run(handler.handle(_cmd()))
    assert len(bus.sent) == 1
    assert second.outcome is EnumTenantOutboxFlushOutcome.NOTHING_TO_FLUSH
    assert second.published == 0


def test_a_failed_publish_leaves_the_row_unmarked_and_records_the_error() -> None:
    store, bus = FakeStore(_rows("alpha", "bravo")), FakeBus(refuse={"alpha"})
    result = asyncio.run(_handler(store, bus).handle(_cmd()))
    assert result.outcome is EnumTenantOutboxFlushOutcome.PARTIAL
    assert (result.published, result.failed) == (1, 1)
    assert "1" not in store.published
    assert "2" in store.published
    assert "TimeoutError" in store.failures["1"]


def test_a_failed_row_is_sent_on_the_next_flush() -> None:
    store = FakeStore(_rows("alpha"))
    asyncio.run(_handler(store, FakeBus(refuse={"alpha"})).handle(_cmd()))
    bus = FakeBus()
    result = asyncio.run(_handler(store, bus).handle(_cmd()))
    assert result.outcome is EnumTenantOutboxFlushOutcome.FLUSHED
    assert len(bus.sent) == 1
    assert "1" in store.published


def test_batch_limit_bounds_one_flush() -> None:
    store, bus = FakeStore(_rows("alpha", "bravo", "charlie")), FakeBus()
    result = asyncio.run(_handler(store, bus).handle(_cmd(limit=2)))
    assert result.published == 2
    assert len(bus.sent) == 2


def test_message_key_is_the_tenant_id_and_value_is_the_envelope() -> None:
    rows = _rows("alpha")
    store, bus = FakeStore(rows), FakeBus()
    asyncio.run(_handler(store, bus).handle(_cmd()))
    _, key, value = bus.sent[0]
    assert key == rows[0].envelope["payload"]["tenant"]["tenant_id"]
    assert dict(value) == rows[0].envelope


def test_unreadable_store_is_a_typed_outcome_and_publishes_nothing() -> None:
    store, bus = FakeStore(_rows("alpha")), FakeBus()
    store.fail_reads = True
    result = asyncio.run(_handler(store, bus).handle(_cmd()))
    assert result.outcome is EnumTenantOutboxFlushOutcome.FAILED_STORE_UNAVAILABLE
    assert bus.sent == []


def test_a_mark_that_fails_after_the_ack_is_counted_and_not_hidden() -> None:
    store, bus = FakeStore(_rows("alpha")), FakeBus()
    store.fail_marks = True
    result = asyncio.run(_handler(store, bus).handle(_cmd()))
    assert result.outcome is EnumTenantOutboxFlushOutcome.PARTIAL
    assert result.mark_failed == 1
    assert result.published == 0
    assert len(bus.sent) == 1


def test_a_row_with_no_envelope_tenant_is_keyed_by_row_id() -> None:
    row = ModelOutboxRow(
        row_id="9", envelope={"operation": "X", "payload": {}}, attempts=0
    )

    class Bus(FakeBus):
        async def publish(self, topic: str, key: str, value: Mapping[str, Any]) -> None:
            self.sent.append((topic, key, value))

    store, bus = FakeStore([row]), Bus()
    asyncio.run(_handler(store, bus).handle(_cmd()))
    assert bus.sent[0][1] == "9"


def test_the_runtime_builds_the_handler_bare_and_it_answers_without_a_store() -> None:
    """The runtime's auto-wiring constructs the handler with no arguments; with no store and bus it answers a typed outcome."""
    handler = HandlerTenantOutboxFlush()
    result = asyncio.run(
        handler.handle(ModelTenantOutboxFlushCommand(command_id=uuid4(), batch_limit=5))
    )
    assert result.outcome is EnumTenantOutboxFlushOutcome.FAILED_STORE_UNAVAILABLE
