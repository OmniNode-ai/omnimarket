# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Answers ``tenant-plan-assign-requested``: the plan becomes the tenant's one active plan, the entitlement is read back."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_tenant_plan_assign_effect.handlers.tenant_plan_postgres import (
    EnumPlanAssignKind,
    TenantPlanPostgres,
    TenantPlanStoreUnavailableError,
)
from omnimarket.nodes.node_tenant_plan_assign_effect.models.model_tenant_plan_assign import (
    EnumTenantPlanAssignOutcome,
    ModelTenantPlanAssignCommand,
    ModelTenantPlanAssignResult,
)
from omnimarket.nodes.node_tenant_plan_assign_effect.overlay import (
    build_store,
    load_overlay,
)

_OUTCOME = EnumTenantPlanAssignOutcome


def utc_now() -> datetime:
    return datetime.now(UTC)


class HandlerTenantPlanAssign:
    """Constructed by the runtime from the contract; the store comes from the overlay unless one is injected."""

    def __init__(
        self,
        store: TenantPlanPostgres | None = None,
        *,
        clock: Callable[[], datetime] = utc_now,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._environ: Mapping[str, str] = (
            environ if environ is not None else os.environ
        )

    def _store_or_build(self) -> TenantPlanPostgres:
        if self._store is None:
            self._store = build_store(load_overlay(self._environ))
        return self._store

    def _answer(
        self,
        request: ModelTenantPlanAssignCommand,
        outcome: EnumTenantPlanAssignOutcome,
        detail: str,
    ) -> ModelTenantPlanAssignResult:
        return ModelTenantPlanAssignResult(
            command_id=request.command_id,
            outcome=outcome,
            detail=detail,
            emitted_at=self._clock(),
        )

    async def handle(
        self, request: ModelTenantPlanAssignCommand
    ) -> ModelTenantPlanAssignResult:
        try:
            record = await self._store_or_build().assign(
                tenant_id=request.tenant_id, plan_code=request.plan_code
            )
        except TenantPlanStoreUnavailableError as exc:
            return self._answer(
                request,
                _OUTCOME.FAILED_STORE_UNAVAILABLE,
                f"identity store unavailable: {exc}",
            )
        except NodeOverlayError as exc:
            return self._answer(
                request,
                _OUTCOME.FAILED_STORE_UNAVAILABLE,
                f"identity store unavailable: overlay not loaded: {exc}",
            )
        if record.kind == EnumPlanAssignKind.UNKNOWN_TENANT:
            return self._answer(
                request, _OUTCOME.REFUSED_UNKNOWN_TENANT, "no tenant with that id"
            )
        if record.kind == EnumPlanAssignKind.UNKNOWN_PLAN:
            return self._answer(
                request, _OUTCOME.REFUSED_UNKNOWN_PLAN, "no plan with that code"
            )
        assigned = record.kind == EnumPlanAssignKind.ASSIGNED
        return ModelTenantPlanAssignResult(
            command_id=request.command_id,
            outcome=_OUTCOME.ASSIGNED if assigned else _OUTCOME.ALREADY_ASSIGNED,
            detail="plan assigned" if assigned else "plan already the active plan",
            tenant_id=request.tenant_id,
            plan_code=request.plan_code,
            previous_plan_code=record.previous_plan_code,
            entitlements=record.entitlements,
            emitted_at=self._clock(),
        )


__all__ = ["HandlerTenantPlanAssign"]
