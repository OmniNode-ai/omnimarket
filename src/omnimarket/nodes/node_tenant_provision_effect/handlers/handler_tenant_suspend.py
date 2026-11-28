# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Answers ``tenant-suspend-requested``: set one tenant's ``provisioning_state`` to ``suspended``, the retirement of a tenant.

The tenant is named by ``tenant_id`` or by ``tenant_slug``, never both, never neither. A tenant that still holds a live
gateway key or a live provider key is refused with the counts and nothing is written: the keys are revoked first, by
their own nodes. A tenant already suspended answers ``already_suspended`` and no row changes, so a replayed command is safe.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import datetime

from omnimarket.nodes.node_tenant_provision_effect.handlers.handler_tenant_provision import (
    utc_now,
)
from omnimarket.nodes.node_tenant_provision_effect.handlers.tenant_identity_postgres import (
    EnumSuspendKind,
    TenantIdentityPostgres,
    TenantStoreUnavailableError,
)
from omnimarket.nodes.node_tenant_provision_effect.models.model_tenant_provision import (
    EnumTenantSuspendOutcome,
    ModelTenantSuspendCommand,
    ModelTenantSuspendResult,
)
from omnimarket.nodes.node_tenant_provision_effect.overlay import (
    build_store,
    load_overlay,
)

_OUTCOME_BY_KIND = {
    EnumSuspendKind.SUSPENDED: (EnumTenantSuspendOutcome.SUSPENDED, "tenant suspended"),
    EnumSuspendKind.ALREADY_SUSPENDED: (
        EnumTenantSuspendOutcome.ALREADY_SUSPENDED,
        "tenant was already suspended",
    ),
    EnumSuspendKind.LIVE_KEYS: (
        EnumTenantSuspendOutcome.REFUSED_LIVE_KEYS,
        "tenant holds live keys; revoke them first",
    ),
    EnumSuspendKind.NOT_FOUND: (
        EnumTenantSuspendOutcome.NOT_FOUND,
        "no tenant with that id or slug",
    ),
}


class HandlerTenantSuspend:
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

    def _answer(
        self,
        request: ModelTenantSuspendCommand,
        outcome: EnumTenantSuspendOutcome,
        detail: str,
    ) -> ModelTenantSuspendResult:
        return ModelTenantSuspendResult(
            command_id=request.command_id,
            outcome=outcome,
            detail=detail,
            emitted_at=self._clock(),
        )

    async def handle(
        self, request: ModelTenantSuspendCommand
    ) -> ModelTenantSuspendResult:
        if (request.tenant_id is None) == (request.tenant_slug is None):
            return self._answer(
                request,
                EnumTenantSuspendOutcome.REFUSED_INVALID_REQUEST,
                "name exactly one of tenant_id and tenant_slug",
            )
        try:
            record = await self._store_or_build().suspend(
                tenant_id=request.tenant_id, tenant_slug=request.tenant_slug
            )
        except TenantStoreUnavailableError as exc:
            return self._answer(
                request,
                EnumTenantSuspendOutcome.FAILED_STORE_UNAVAILABLE,
                f"identity store unavailable: {exc}",
            )
        outcome, detail = _OUTCOME_BY_KIND[record.kind]
        return ModelTenantSuspendResult(
            command_id=request.command_id,
            outcome=outcome,
            detail=detail,
            tenant_id=record.tenant_id,
            tenant_slug=record.tenant_slug,
            provisioning_state=record.provisioning_state,
            live_gateway_keys=record.live_gateway_keys,
            live_provider_keys=record.live_provider_keys,
            emitted_at=self._clock(),
        )


__all__ = ["HandlerTenantSuspend"]
