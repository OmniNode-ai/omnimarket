# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain proof for node_tenant_outbox_flush_effect: the contract's golden path, link by link.

The chain: a flush command on the requested topic -> ``HandlerTenantOutboxFlush`` (the handler the contract routes to)
-> the TENANT_CREATED envelope the provision node wrote, published to the topic the contract declares -> the envelope the
tenant registry projection parses through its own parser -> the row marked published, so a second flush sends nothing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from importlib import import_module, resources
from typing import Any
from uuid import UUID

import pytest
import yaml

from omnimarket.nodes.node_tenant_outbox_flush_effect.handlers.handler_tenant_outbox_flush import (
    HandlerTenantOutboxFlush,
    publish_topic_from_contract,
)
from omnimarket.nodes.node_tenant_outbox_flush_effect.models.model_tenant_outbox_flush import (
    EnumTenantOutboxFlushOutcome,
    ModelOutboxRow,
    ModelTenantOutboxFlushCommand,
)
from omnimarket.nodes.node_tenant_provision_effect.handlers.tenant_created_envelope import (
    build_tenant_created_envelope,
)
from omnimarket.projection.tenant_registry_events import ModelTenantRegistryEvent

pytestmark = pytest.mark.unit

_NODE = "node_tenant_outbox_flush_effect"
TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")


class _Store:
    def __init__(self, row: ModelOutboxRow) -> None:
        self._row = row
        self.marked = False

    async def fetch_unpublished(self, limit: int) -> Sequence[ModelOutboxRow]:
        return [] if self.marked else [self._row]

    async def mark_published(self, row_id: str, published_at: datetime) -> None:
        self.marked = True

    async def record_failure(self, row_id: str, error: str) -> None:
        raise AssertionError("no failure expected")


class _Bus:
    def __init__(self) -> None:
        self.sent: list[tuple[str, Mapping[str, Any]]] = []

    async def publish(self, topic: str, key: str, value: Mapping[str, Any]) -> None:
        self.sent.append((topic, value))


def _contract() -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{_NODE}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    loaded: dict[str, Any] = yaml.safe_load(text)
    return loaded


def test_the_contract_routes_the_flush_topic_to_the_handler() -> None:
    contract = _contract()
    entry = contract["handler_routing"]["handlers"][0]
    handler_class = getattr(
        import_module(entry["handler"]["module"]), entry["handler"]["name"]
    )
    assert handler_class is HandlerTenantOutboxFlush
    assert entry["topic"] in contract["event_bus"]["subscribe_topics"]
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]


def test_golden_path_the_provisioned_envelope_reaches_the_registry_parser_once() -> (
    None
):
    envelope = build_tenant_created_envelope(
        command_id=UUID("11111111-1111-4111-8111-111111111111"),
        tenant_id=TENANT_ID,
        tenant_slug="acme-labs",
        name="Acme Labs",
        status="active",
        created_at=datetime(2026, 11, 22, 21, 0, 0, tzinfo=UTC),
        plan_code="discovery",
    )
    store, bus = _Store(ModelOutboxRow(row_id="1", envelope=envelope)), _Bus()
    handler = HandlerTenantOutboxFlush(store, bus, topic=publish_topic_from_contract())
    command = ModelTenantOutboxFlushCommand(command_id=UUID(int=7))

    first = asyncio.run(handler.handle(command))
    assert first.outcome is EnumTenantOutboxFlushOutcome.FLUSHED
    topic, value = bus.sent[0]
    assert topic == _contract()["topics"]["flush"]["tenant_events"]
    parsed = ModelTenantRegistryEvent.from_envelope(dict(value))
    assert parsed.tenant.tenant_id == TENANT_ID

    second = asyncio.run(handler.handle(command))
    assert second.outcome is EnumTenantOutboxFlushOutcome.NOTHING_TO_FLUSH
    assert len(bus.sent) == 1
