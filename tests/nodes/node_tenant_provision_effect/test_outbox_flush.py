# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The outbox flush: every unpublished TENANT_CREATED row is sent to its topic and marked published, once.

Measured on the dev lane: the provision node wrote the outbox row and nothing ever published it, so the tenant registry
mirror never learned of the tenant. The flush is a third route of this node, triggered by the provision completed event
and by its own command topic.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_tenant_provision_effect.handlers.handler_tenant_outbox_flush import (
    HandlerTenantOutboxFlush,
)
from omnimarket.nodes.node_tenant_provision_effect.handlers.tenant_identity_postgres import (
    OutboxRow,
    TenantStoreUnavailableError,
)
from omnimarket.nodes.node_tenant_provision_effect.models.model_tenant_provision import (
    EnumTenantOutboxFlushOutcome,
    ModelTenantOutboxFlushCommand,
)

pytestmark = pytest.mark.asyncio

TOPIC = "onex.tenant.events"  # onex-topic-allow: control-plane tenant lifecycle topic the outbox flush writes


def _envelope(n: int) -> dict[str, Any]:
    return {
        "operation": "TENANT_CREATED",
        "correlation_id": str(uuid4()),
        "payload": {"tenant": {"tenant_slug": f"acme-{n}"}},
    }


class FakeStore:
    def __init__(self, rows: list[OutboxRow], down: bool = False) -> None:
        self.rows = {r.row_id: r for r in rows}
        self.published: list[int] = []
        self.failed: dict[int, str] = {}
        self.down = down

    async def pending_outbox(self, limit: int) -> list[OutboxRow]:
        if self.down:
            raise TenantStoreUnavailableError("OSError")
        pending = [
            r
            for r in self.rows.values()
            if r.row_id not in self.published and r.row_id not in self.failed
        ]
        return pending[:limit]

    async def mark_outbox_published(self, row_id: int) -> None:
        self.published.append(row_id)

    async def mark_outbox_failed(self, row_id: int, error: str) -> None:
        self.failed[row_id] = error


class Sent:
    def __init__(self, fail_on: int | None = None) -> None:
        self.messages: list[tuple[str, dict[str, Any]]] = []
        self.fail_on = fail_on
        self.calls = 0

    async def __call__(self, topic: str, payload: bytes) -> None:
        body = json.loads(payload)
        self.calls += 1
        if self.fail_on is not None and self.calls - 1 == self.fail_on:
            raise ConnectionError("broker refused")
        self.messages.append((topic, body))


def _handler(store: FakeStore, sent: Sent) -> HandlerTenantOutboxFlush:
    return HandlerTenantOutboxFlush(
        store=store,  # type: ignore[arg-type]
        publisher=sent,
        clock=lambda: datetime(2026, 11, 26, tzinfo=UTC),
    )


async def test_every_pending_row_is_published_to_its_topic_and_marked() -> None:
    rows = [OutboxRow(row_id=i, topic=TOPIC, envelope=_envelope(i)) for i in (1, 2)]
    store, sent = FakeStore(rows), Sent()
    answer = await _handler(store, sent).handle(ModelTenantOutboxFlushCommand())
    assert answer.outcome is EnumTenantOutboxFlushOutcome.FLUSHED
    assert (answer.published, answer.failed) == (2, 0)
    assert [t for t, _ in sent.messages] == [TOPIC, TOPIC]
    assert [b for _, b in sent.messages] == [r.envelope for r in rows]
    assert store.published == [1, 2]


async def test_known_good_and_known_bad_second_run_publishes_nothing_twice() -> None:
    store, sent = FakeStore([OutboxRow(1, TOPIC, _envelope(1))]), Sent()
    handler = _handler(store, sent)
    first = await handler.handle(ModelTenantOutboxFlushCommand())
    second = await handler.handle(ModelTenantOutboxFlushCommand())
    assert first.published == 1
    assert second.outcome is EnumTenantOutboxFlushOutcome.NOTHING_PENDING
    assert len(sent.messages) == 1


async def test_a_refused_publish_is_recorded_and_the_row_stays_unpublished() -> None:
    rows = [OutboxRow(i, TOPIC, _envelope(i)) for i in (1, 2)]
    store, sent = FakeStore(rows), Sent(fail_on=0)
    answer = await _handler(store, sent).handle(ModelTenantOutboxFlushCommand())
    assert answer.outcome is EnumTenantOutboxFlushOutcome.PARTIAL
    assert (answer.published, answer.failed) == (1, 1)
    assert store.published == [2]
    assert store.failed == {1: "ConnectionError"}


async def test_store_down_is_a_typed_outcome() -> None:
    answer = await _handler(FakeStore([], down=True), Sent()).handle(
        ModelTenantOutboxFlushCommand()
    )
    assert answer.outcome is EnumTenantOutboxFlushOutcome.FAILED_STORE_UNAVAILABLE


async def test_the_provision_completed_event_parses_as_a_flush_trigger() -> None:
    event = {
        "command_id": str(uuid4()),
        "outcome": "created",
        "detail": "tenant created",
        "tenant_slug": "acme-labs",
    }
    command = ModelTenantOutboxFlushCommand.model_validate(event)
    assert command.limit == 50


async def test_no_overlay_is_a_typed_outcome(tmp_path: Path) -> None:
    handler = HandlerTenantOutboxFlush(
        environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
    )
    answer = await handler.handle(ModelTenantOutboxFlushCommand())
    assert answer.outcome is EnumTenantOutboxFlushOutcome.FAILED_STORE_UNAVAILABLE
    assert "overlay" in answer.detail
