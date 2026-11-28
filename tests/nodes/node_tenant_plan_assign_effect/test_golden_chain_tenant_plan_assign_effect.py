# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain proof for node_tenant_plan_assign_effect: the contract's golden path, link by link.

The chain: an assign command on the requested topic -> ``HandlerTenantPlanAssign`` (the handler the contract's
``topic_match`` routing names) -> an assigned answer on the completed topic carrying the entitlements read back -> an
unknown plan answered as a typed refusal. The store is an in-memory stand-in with the real store's answers; the
scratch-Postgres proof of the store itself is in ``test_plan_assign_postgres.py``.
"""

from __future__ import annotations

from importlib import import_module, resources
from typing import Any, cast
from uuid import UUID

import pytest
import yaml

from omnimarket.nodes.node_tenant_plan_assign_effect.handlers.handler_tenant_plan_assign import (
    HandlerTenantPlanAssign,
)
from omnimarket.nodes.node_tenant_plan_assign_effect.handlers.tenant_plan_postgres import (
    EnumPlanAssignKind,
    PlanAssignRecord,
    TenantPlanPostgres,
)
from omnimarket.nodes.node_tenant_plan_assign_effect.models.model_tenant_plan_assign import (
    ModelPlanEntitlement,
    ModelTenantPlanAssignCommand,
)

pytestmark = pytest.mark.unit

_NODE = "node_tenant_plan_assign_effect"
COMMAND_ID = UUID("11111111-1111-4111-8111-111111111111")
TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")


class _InMemoryStore:
    async def assign(self, *, tenant_id: UUID, plan_code: str) -> PlanAssignRecord:
        if plan_code != "build":
            return PlanAssignRecord(kind=EnumPlanAssignKind.UNKNOWN_PLAN)
        return PlanAssignRecord(
            kind=EnumPlanAssignKind.ASSIGNED,
            previous_plan_code="discovery",
            entitlements=(
                ModelPlanEntitlement(
                    metric_key="workflow_submits",
                    window_key="month",
                    limit_value=100000,
                    hard_cap=True,
                ),
            ),
        )


def _contract() -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{_NODE}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    loaded: dict[str, Any] = yaml.safe_load(text)
    return loaded


def test_the_contract_routes_the_command_topic_to_the_handler() -> None:
    contract = _contract()
    (entry,) = contract["handler_routing"]["handlers"]
    handler_class = getattr(
        import_module(entry["handler"]["module"]), entry["handler"]["name"]
    )
    assert handler_class is HandlerTenantPlanAssign
    assert entry["topic"] in contract["event_bus"]["subscribe_topics"]
    assert (
        contract["topics"]["tenant_plan_assign"]["completed"]
        == contract["terminal_event"]
    )


@pytest.mark.asyncio
async def test_golden_path_assigned_with_entitlements_then_unknown_plan_refused() -> (
    None
):
    handler = HandlerTenantPlanAssign(store=cast(TenantPlanPostgres, _InMemoryStore()))
    ok = await handler.handle(
        ModelTenantPlanAssignCommand(
            command_id=COMMAND_ID, tenant_id=TENANT_ID, plan_code="build"
        )
    )
    assert ok.outcome.value == "assigned"
    assert ok.previous_plan_code == "discovery"
    assert ok.entitlements[0].limit_value == 100000

    refused = await handler.handle(
        ModelTenantPlanAssignCommand(
            command_id=COMMAND_ID, tenant_id=TENANT_ID, plan_code="nope"
        )
    )
    assert refused.outcome.value == "refused_unknown_plan"
    assert refused.entitlements == ()
