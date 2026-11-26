# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The outbox flush publishes each unpublished tenant envelope and marks it published only after the publish."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_tenant_event_outbox_flush_effect.handlers.handler_tenant_outbox_flush import (
    HandlerTenantOutboxFlush,
)
from omnimarket.nodes.node_tenant_event_outbox_flush_effect.models.model_tenant_outbox_flush import (
    EnumFlushOutcome,
    ModelOutboxRow,
    ModelTenantOutboxFlushCommand,
)
from omnimarket.nodes.node_tenant_provision_effect.handlers.tenant_created_envelope import (
    TENANT_EVENTS_TOPIC,
    build_tenant_created_envelope,
)
from omnimarket.projection.tenant_registry_events import ModelTenantRegistryEvent


def _envelope() -> dict[str, Any]:
    from datetime import UTC, datetime

    return build_tenant_created_envelope(
        command_id=uuid4(),
        tenant_id=uuid4(),
        tenant_slug="lab-gw-a",
        name=None,
        status="active",
        created_at=datetime.now(UTC),
        plan_code="free",
    )


class FakeStore:
    def __init__(self, rows: list[ModelOutboxRow]) -> None:
        self.rows = rows
        self.published: list[int] = []
        self.failed: list[tuple[int, str]] = []

    async def pending(self, limit: int) -> list[ModelOutboxRow]:
        return [r for r in self.rows if r.id not in self.published][:limit]

    async def mark_published(self, row_id: int) -> None:
        self.published.append(row_id)

    async def mark_failed(self, row_id: int, error_class: str) -> None:
        self.failed.append((row_id, error_class))


class FakePublisher:
    def __init__(self, fail_on: set[str] | None = None) -> None:
        self.sent: list[tuple[str, str, dict[str, Any]]] = []
        self.fail_on = fail_on or set()

    async def publish(self, *, topic: str, key: str, record: dict[str, Any]) -> None:
        if key in self.fail_on:
            raise RuntimeError("broker down")
        self.sent.append((topic, key, record))


@pytest.mark.unit
async def test_known_good_flushes_each_pending_row_to_the_tenant_topic() -> None:
    rows = [
        ModelOutboxRow(id=1, topic=TENANT_EVENTS_TOPIC, envelope=_envelope()),
        ModelOutboxRow(id=2, topic=TENANT_EVENTS_TOPIC, envelope=_envelope()),
    ]
    store, pub = FakeStore(rows), FakePublisher()
    result = await HandlerTenantOutboxFlush(store, pub).handle(
        ModelTenantOutboxFlushCommand(limit=10)
    )
    assert result.outcome is EnumFlushOutcome.FLUSHED
    assert (result.published, result.failed) == (2, 0)
    assert store.published == [1, 2]
    assert [t for t, _, _ in pub.sent] == [TENANT_EVENTS_TOPIC] * 2
    # what is published parses through the registry projection's own parser
    ModelTenantRegistryEvent.from_envelope(pub.sent[0][2])


@pytest.mark.unit
async def test_known_bad_publish_failure_leaves_the_row_unpublished() -> None:
    env = _envelope()
    rows = [ModelOutboxRow(id=7, topic=TENANT_EVENTS_TOPIC, envelope=env)]
    store = FakeStore(rows)
    pub = FakePublisher(fail_on={env["correlation_id"]})
    result = await HandlerTenantOutboxFlush(store, pub).handle(
        ModelTenantOutboxFlushCommand(limit=10)
    )
    assert result.outcome is EnumFlushOutcome.PUBLISH_FAILED
    assert (result.published, result.failed) == (0, 1)
    assert store.published == []
    assert store.failed == [(7, "RuntimeError")]


@pytest.mark.unit
async def test_nothing_pending_is_a_typed_answer() -> None:
    result = await HandlerTenantOutboxFlush(FakeStore([]), FakePublisher()).handle(
        ModelTenantOutboxFlushCommand(limit=10)
    )
    assert result.outcome is EnumFlushOutcome.NOTHING_PENDING
