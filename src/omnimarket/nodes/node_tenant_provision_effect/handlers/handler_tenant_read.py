# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Answers ``tenant-read-requested``: one tenant by ``tenant_id`` or by ``tenant_slug``, never both, never neither."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import datetime

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_tenant_provision_effect.handlers.handler_tenant_provision import (
    utc_now,
)
from omnimarket.nodes.node_tenant_provision_effect.handlers.tenant_identity_postgres import (
    TenantIdentityPostgres,
    TenantStoreUnavailableError,
)
from omnimarket.nodes.node_tenant_provision_effect.models.model_tenant_provision import (
    EnumTenantReadOutcome,
    ModelTenantReadCommand,
    ModelTenantReadResult,
)
from omnimarket.nodes.node_tenant_provision_effect.overlay import (
    build_store,
    load_overlay,
)


class HandlerTenantRead:
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
        request: ModelTenantReadCommand,
        outcome: EnumTenantReadOutcome,
        detail: str,
    ) -> ModelTenantReadResult:
        return ModelTenantReadResult(
            command_id=request.command_id,
            outcome=outcome,
            detail=detail,
            emitted_at=self._clock(),
        )

    async def handle(self, request: ModelTenantReadCommand) -> ModelTenantReadResult:
        if (request.tenant_id is None) == (request.tenant_slug is None):
            return self._answer(
                request,
                EnumTenantReadOutcome.REFUSED_INVALID_REQUEST,
                "name exactly one of tenant_id and tenant_slug",
            )
        try:
            row = await self._store_or_build().read(
                tenant_id=request.tenant_id, tenant_slug=request.tenant_slug
            )
        except TenantStoreUnavailableError as exc:
            return self._answer(
                request,
                EnumTenantReadOutcome.FAILED_STORE_UNAVAILABLE,
                f"identity store unavailable: {exc}",
            )
        except NodeOverlayError as exc:
            return self._answer(
                request,
                EnumTenantReadOutcome.FAILED_STORE_UNAVAILABLE,
                f"identity store unavailable: overlay not loaded: {exc}",
            )
        if row is None:
            return self._answer(
                request,
                EnumTenantReadOutcome.NOT_FOUND,
                "no tenant with that id or slug",
            )
        return ModelTenantReadResult(
            command_id=request.command_id,
            outcome=EnumTenantReadOutcome.FOUND,
            detail="tenant found",
            tenant_id=row.tenant_id,
            tenant_slug=row.tenant_slug,
            name=row.name,
            provisioning_state=row.provisioning_state,
            plan_code=row.plan_code,
            created_at=row.created_at,
            emitted_at=self._clock(),
        )


__all__ = ["HandlerTenantRead"]
