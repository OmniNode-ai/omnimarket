# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command and answer of the plan-assign node.

The command names a tenant and a plan code. The answer carries a typed ``outcome`` and, for ``assigned`` and
``already_assigned``, the entitlement rows of the plan read back after the write, so the caller's one read shows the quota.
Every refusal and every handled error is an outcome on the ``-completed`` topic.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EnumTenantPlanAssignOutcome(StrEnum):
    """What an assign command ended as; every value but ``assigned`` wrote nothing."""

    ASSIGNED = "assigned"
    ALREADY_ASSIGNED = "already_assigned"
    REFUSED_UNKNOWN_PLAN = "refused_unknown_plan"
    REFUSED_UNKNOWN_TENANT = "refused_unknown_tenant"
    FAILED_STORE_UNAVAILABLE = "failed_store_unavailable"


class ModelTenantPlanAssignCommand(BaseModel):
    """Make ``plan_code`` the one active plan of ``tenant_id``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: UUID
    tenant_id: UUID
    plan_code: str = Field(min_length=1)


class ModelPlanEntitlement(BaseModel):
    """One ``plan_entitlements`` row of the assigned plan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    metric_key: str
    window_key: str
    limit_value: int | None = None
    hard_cap: bool | None = None
    unit: str | None = None


class ModelTenantPlanAssignResult(BaseModel):
    """The answer on ``-completed``. Plan fields are set for ``assigned`` and ``already_assigned`` only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: UUID
    outcome: EnumTenantPlanAssignOutcome
    detail: str
    tenant_id: UUID | None = None
    plan_code: str | None = None
    previous_plan_code: str | None = None
    entitlements: tuple[ModelPlanEntitlement, ...] = ()
    emitted_at: datetime


__all__ = [
    "EnumTenantPlanAssignOutcome",
    "ModelPlanEntitlement",
    "ModelTenantPlanAssignCommand",
    "ModelTenantPlanAssignResult",
]
