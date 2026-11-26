# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The store's outbox methods against a scratch PostgreSQL 16 holding the dev identity tables."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from omnimarket.nodes.node_tenant_provision_effect.handlers.handler_tenant_outbox_flush import (
    HandlerTenantOutboxFlush,
)
from omnimarket.nodes.node_tenant_provision_effect.handlers.handler_tenant_provision import (
    HandlerTenantProvision,
)
from omnimarket.nodes.node_tenant_provision_effect.handlers.tenant_identity_postgres import (
    TenantIdentityPostgres,
)
from omnimarket.nodes.node_tenant_provision_effect.models.model_tenant_provision import (
    EnumTenantOutboxFlushOutcome,
    ModelTenantOutboxFlushCommand,
    ModelTenantProvisionCommand,
)
from omnimarket.projection.tenant_registry_events import ModelTenantRegistryEvent

from .postgres_support import identity_tables

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_provision_then_flush_publishes_the_row_the_registry_reads_once(
    port: int,
) -> None:
    sent: list[tuple[str, bytes]] = []

    async def publisher(topic: str, payload: bytes) -> None:
        sent.append((topic, payload))

    async with identity_tables(port) as (admin, pool):
        store = TenantIdentityPostgres(pool=pool)
        await HandlerTenantProvision(store=store).handle(
            ModelTenantProvisionCommand(command_id=uuid4(), tenant_slug="acme-labs")
        )
        assert (
            await admin.fetchval(
                "SELECT count(*) FROM tenant_event_outbox WHERE published_at IS NULL"
            )
            == 1
        )
        flush = HandlerTenantOutboxFlush(store=store, publisher=publisher)
        first = await flush.handle(ModelTenantOutboxFlushCommand())
        second = await flush.handle(ModelTenantOutboxFlushCommand())
        assert first.outcome is EnumTenantOutboxFlushOutcome.FLUSHED
        assert second.outcome is EnumTenantOutboxFlushOutcome.NOTHING_PENDING
        assert len(sent) == 1
        topic, payload = sent[0]
        assert (
            topic == "onex.tenant.events"
        )  # onex-topic-allow: control-plane tenant lifecycle topic the outbox flush writes
        event = ModelTenantRegistryEvent.from_envelope(json.loads(payload))
        assert event.operation == "TENANT_CREATED"
        row = await admin.fetchrow(
            "SELECT published_at, attempts, last_error FROM tenant_event_outbox"
        )
        assert row is not None
        assert row["published_at"] is not None
        assert row["last_error"] is None
