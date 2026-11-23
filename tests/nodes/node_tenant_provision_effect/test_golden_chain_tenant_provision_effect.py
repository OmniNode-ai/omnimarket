# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain proof for node_tenant_provision_effect: the contract's golden path, link by link.

The chain: a provision command on the requested topic -> ``HandlerTenantProvision`` (the handler the contract's
``topic_match`` routing names) -> a created answer on the completed topic -> the TENANT_CREATED envelope the registry
projection parses through its own parser -> the same command id replayed answers a payload equal to the first except
``emitted_at``. The store is an in-memory stand-in with the real store's two answers (created, then replay); the
scratch-Postgres proof of the store itself is in ``test_provision_postgres.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from importlib import import_module, resources
from typing import Any, cast
from uuid import UUID

import pytest
import yaml

from omnimarket.nodes.node_tenant_provision_effect.handlers.handler_tenant_provision import (
    HandlerTenantProvision,
)
from omnimarket.nodes.node_tenant_provision_effect.handlers.tenant_created_envelope import (
    build_tenant_created_envelope,
)
from omnimarket.nodes.node_tenant_provision_effect.handlers.tenant_identity_postgres import (
    EnumProvisionKind,
    ProvisionRecord,
    TenantIdentityPostgres,
)
from omnimarket.nodes.node_tenant_provision_effect.models.model_tenant_provision import (
    ModelTenantProvisionCommand,
)
from omnimarket.projection.tenant_registry_events import ModelTenantRegistryEvent

pytestmark = pytest.mark.unit

_NODE = "node_tenant_provision_effect"
COMMAND_ID = UUID("11111111-1111-4111-8111-111111111111")
TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")
CREATED_AT = datetime(2026, 11, 22, 21, 0, 0, tzinfo=UTC)


class _InMemoryStore:
    """Answers created for the first command and the stored envelope as a replay for the next."""

    def __init__(self) -> None:
        self._envelope: dict[str, Any] | None = None

    async def provision(self, request: ModelTenantProvisionCommand) -> ProvisionRecord:
        if self._envelope is None:
            self._envelope = build_tenant_created_envelope(
                command_id=request.command_id,
                tenant_id=TENANT_ID,
                tenant_slug=request.tenant_slug,
                name=request.name,
                status="active",
                created_at=CREATED_AT,
                plan_code="discovery",
            )
            return ProvisionRecord(
                kind=EnumProvisionKind.CREATED, envelope=self._envelope
            )
        return ProvisionRecord(kind=EnumProvisionKind.REPLAY, envelope=self._envelope)


def _contract() -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{_NODE}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    loaded: dict[str, Any] = yaml.safe_load(text)
    return loaded


def test_the_contract_routes_the_provision_topic_to_the_handler_and_names_the_answer_topic() -> (
    None
):
    contract = _contract()
    entry = next(
        e
        for e in contract["handler_routing"]["handlers"]
        if e["operation"] == "tenant_provision"
    )
    handler_class = getattr(
        import_module(entry["handler"]["module"]), entry["handler"]["name"]
    )
    assert handler_class is HandlerTenantProvision
    assert entry["topic"] in contract["event_bus"]["subscribe_topics"]
    assert (
        contract["topics"]["tenant_provision"]["completed"]
        in contract["event_bus"]["publish_topics"]
    )
    assert (
        contract["terminal_event"]
        == contract["topics"]["tenant_provision"]["completed"]
    )


@pytest.mark.asyncio
async def test_golden_path_created_envelope_parses_and_a_replay_answers_the_same_payload() -> (
    None
):
    store = cast(TenantIdentityPostgres, _InMemoryStore())
    handler = HandlerTenantProvision(store=store)
    command = ModelTenantProvisionCommand(
        command_id=COMMAND_ID, tenant_slug="acme-labs", name="Acme Labs"
    )

    first = await handler.handle(command)
    assert first.outcome.value == "created"
    assert first.tenant_id == TENANT_ID

    replay = await handler.handle(command)
    assert replay.model_dump(exclude={"emitted_at"}) == first.model_dump(
        exclude={"emitted_at"}
    )

    envelope = build_tenant_created_envelope(
        command_id=COMMAND_ID,
        tenant_id=TENANT_ID,
        tenant_slug="acme-labs",
        name="Acme Labs",
        status="active",
        created_at=CREATED_AT,
        plan_code="discovery",
    )
    parsed = ModelTenantRegistryEvent.from_envelope(envelope)
    assert parsed.tenant.tenant_id == first.tenant_id
    assert parsed.tenant.status == "active"
