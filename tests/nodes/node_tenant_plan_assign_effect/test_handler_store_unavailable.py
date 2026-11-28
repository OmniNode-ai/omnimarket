# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A store that cannot be reached, or an overlay that does not load, is a typed outcome, never a missing answer."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_tenant_plan_assign_effect.handlers.handler_tenant_plan_assign import (
    HandlerTenantPlanAssign,
)
from omnimarket.nodes.node_tenant_plan_assign_effect.handlers.tenant_plan_postgres import (
    PlanAssignRecord,
    TenantPlanPostgres,
    TenantPlanStoreUnavailableError,
)
from omnimarket.nodes.node_tenant_plan_assign_effect.models.model_tenant_plan_assign import (
    EnumTenantPlanAssignOutcome,
    ModelTenantPlanAssignCommand,
)

pytestmark = pytest.mark.unit


class _Down(TenantPlanPostgres):
    async def assign(self, *, tenant_id: UUID, plan_code: str) -> PlanAssignRecord:
        raise TenantPlanStoreUnavailableError("OSError")


def _command() -> ModelTenantPlanAssignCommand:
    return ModelTenantPlanAssignCommand(
        command_id=uuid4(), tenant_id=uuid4(), plan_code="build"
    )


async def test_store_down_is_a_typed_failure() -> None:
    result = await HandlerTenantPlanAssign(store=_Down()).handle(_command())
    assert result.outcome == EnumTenantPlanAssignOutcome.FAILED_STORE_UNAVAILABLE
    assert "OSError" in result.detail


async def test_overlay_missing_is_a_typed_failure() -> None:
    result = await HandlerTenantPlanAssign(environ={}).handle(_command())
    assert result.outcome == EnumTenantPlanAssignOutcome.FAILED_STORE_UNAVAILABLE
    assert NodeOverlayError is not None
