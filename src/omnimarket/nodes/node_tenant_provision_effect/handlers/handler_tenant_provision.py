# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Answers ``tenant-provision-requested``: one ownerless tenant, its default plan and one ``TENANT_CREATED`` outbox row.

The command is checked before any statement is sent: owner fields are refused (the owner path does not exist yet) and
a slug that breaks the slug rule is refused. Past those, one transaction decides between created, a replay of the same
command id, an existing tenant of the same owner, a slug taken by another owner and a command id reused for another
tenant. A replay answers the original ``created`` payload built from the stored envelope, so it equals the first answer
except for ``emitted_at``. The outbox row is written and stays unpublished; a later node publishes it.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_tenant_provision_effect.handlers.tenant_identity_postgres import (
    EnumProvisionKind,
    TenantIdentityPostgres,
    TenantStoreUnavailableError,
)
from omnimarket.nodes.node_tenant_provision_effect.models.model_tenant_provision import (
    EnumTenantProvisionOutcome,
    ModelTenantProvisionCommand,
    ModelTenantProvisionResult,
    slug_violation,
)
from omnimarket.nodes.node_tenant_provision_effect.overlay import (
    build_store,
    load_overlay,
)

_CREATED_DETAIL = "tenant created"


def utc_now() -> datetime:
    return datetime.now(UTC)


def _refusal(
    command: ModelTenantProvisionCommand,
    outcome: EnumTenantProvisionOutcome,
    detail: str,
    emitted_at: datetime,
) -> ModelTenantProvisionResult:
    return ModelTenantProvisionResult(
        command_id=command.command_id,
        outcome=outcome,
        detail=detail,
        emitted_at=emitted_at,
    )


def created_result(
    command_id: UUID, envelope: Mapping[str, Any], emitted_at: datetime
) -> ModelTenantProvisionResult:
    """The ``created`` answer, built from the envelope alone so a first answer and a replay agree."""
    tenant = envelope["payload"]["tenant"]
    return ModelTenantProvisionResult(
        command_id=command_id,
        outcome=EnumTenantProvisionOutcome.CREATED,
        detail=_CREATED_DETAIL,
        tenant_id=UUID(tenant["tenant_id"]),
        tenant_slug=tenant["tenant_slug"],
        name=tenant["name"],
        provisioning_state=tenant["status"],
        plan_code=tenant["plan_code"],
        created_at=datetime.fromisoformat(tenant["created_at"]),
        emitted_at=emitted_at,
    )


class HandlerTenantProvision:
    """Constructed by the runtime from the contract; the store comes from the overlay unless one is injected."""

    def __init__(
        self,
        store: TenantIdentityPostgres | None = None,
        *,
        clock: Callable[[], datetime] = utc_now,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._environ: Mapping[str, str] = (
            environ if environ is not None else os.environ
        )

    def _store_or_build(self) -> TenantIdentityPostgres:
        if self._store is None:
            self._store = build_store(load_overlay(self._environ))
        return self._store

    async def handle(
        self, request: ModelTenantProvisionCommand
    ) -> ModelTenantProvisionResult:
        now = self._clock()
        if request.carries_owner():
            return _refusal(
                request,
                EnumTenantProvisionOutcome.REFUSED_OWNER_NOT_SUPPORTED,
                "owner fields are not supported yet; send the command without them",
                now,
            )
        violation = slug_violation(request.tenant_slug)
        if violation is not None:
            return _refusal(
                request, EnumTenantProvisionOutcome.REFUSED_INVALID_SLUG, violation, now
            )
        try:
            record = await self._store_or_build().provision(request)
        except TenantStoreUnavailableError as exc:
            return _refusal(
                request,
                EnumTenantProvisionOutcome.FAILED_STORE_UNAVAILABLE,
                f"identity store unavailable: {exc}",
                now,
            )
        except NodeOverlayError as exc:
            # No overlay on this runtime: answer on the completed topic, so a caller reads a typed outcome, not silence.
            return _refusal(
                request,
                EnumTenantProvisionOutcome.FAILED_STORE_UNAVAILABLE,
                f"identity store unavailable: overlay not loaded: {exc}",
                now,
            )
        match record.kind:
            case EnumProvisionKind.CREATED | EnumProvisionKind.REPLAY:
                assert record.envelope is not None
                return created_result(request.command_id, record.envelope, now)
            case EnumProvisionKind.ALREADY_EXISTS:
                tenant = record.tenant
                assert tenant is not None
                return ModelTenantProvisionResult(
                    command_id=request.command_id,
                    outcome=EnumTenantProvisionOutcome.ALREADY_EXISTS,
                    detail="tenant already exists for this owner; nothing written",
                    tenant_id=tenant.tenant_id,
                    tenant_slug=tenant.tenant_slug,
                    name=tenant.name,
                    provisioning_state=tenant.provisioning_state,
                    plan_code=tenant.plan_code,
                    created_at=tenant.created_at,
                    emitted_at=now,
                )
            case EnumProvisionKind.SLUG_TAKEN:
                return _refusal(
                    request,
                    EnumTenantProvisionOutcome.REFUSED_SLUG_TAKEN,
                    "slug belongs to a tenant with another owner; nothing written",
                    now,
                )
            case EnumProvisionKind.COMMAND_ID_REUSED:
                return _refusal(
                    request,
                    EnumTenantProvisionOutcome.REFUSED_COMMAND_ID_REUSED,
                    "command id already produced a tenant for another slug or owner; nothing written",
                    now,
                )
            case EnumProvisionKind.NO_DEFAULT_PLAN:
                return _refusal(
                    request,
                    EnumTenantProvisionOutcome.FAILED_NO_DEFAULT_PLAN,
                    "no default plan tier exists; transaction rolled back",
                    now,
                )


__all__ = ["HandlerTenantProvision", "created_result", "utc_now"]
