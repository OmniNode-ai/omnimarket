# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Flushes ``tenant_event_outbox``: every unpublished row is sent to the topic it names, then stamped published.

Triggered by ``tenant-provision-completed`` (the row the provision just wrote) and by its own command topic (a retry or a
backlog). A row is stamped only after the bus accepted it; a refused send counts one attempt, keeps the error class and
leaves the row unpublished for the next flush. A flush that finds no row answers ``nothing_pending`` and sends nothing.
The delivery is at least once: the registry projection applies a replayed ``TENANT_CREATED`` as an upsert.
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable, Mapping
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
    EnumTenantOutboxFlushOutcome,
    ModelTenantOutboxFlushCommand,
    ModelTenantOutboxFlushResult,
    ModelTenantProvisionResult,
)
from omnimarket.nodes.node_tenant_provision_effect.overlay import (
    build_publisher,
    build_store,
    load_overlay,
)

Publisher = Callable[[str, bytes], Awaitable[None]]


class HandlerTenantOutboxFlush:
    """Constructed by the runtime from the contract; store and publisher come from the overlay unless injected."""

    def __init__(
        self,
        store: TenantIdentityPostgres | None = None,
        *,
        publisher: Publisher | None = None,
        clock: Callable[[], datetime] = utc_now,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._store = store
        self._publisher = publisher
        self._clock = clock
        self._environ: Mapping[str, str] = (
            environ if environ is not None else os.environ
        )

    def _assemble(self) -> tuple[TenantIdentityPostgres, Publisher]:
        if self._store is None or self._publisher is None:
            overlay = load_overlay(self._environ)
            if self._store is None:
                self._store = build_store(overlay)
            if self._publisher is None:
                self._publisher = build_publisher(overlay)
        return self._store, self._publisher

    def _answer(
        self,
        outcome: EnumTenantOutboxFlushOutcome,
        detail: str,
        published: int = 0,
        failed: int = 0,
    ) -> ModelTenantOutboxFlushResult:
        return ModelTenantOutboxFlushResult(
            outcome=outcome,
            detail=detail,
            published=published,
            failed=failed,
            emitted_at=self._clock(),
        )

    async def handle(
        self, request: ModelTenantOutboxFlushCommand
    ) -> ModelTenantOutboxFlushResult:
        unavailable = EnumTenantOutboxFlushOutcome.FAILED_STORE_UNAVAILABLE
        try:
            store, publisher = self._assemble()
            rows = await store.pending_outbox(request.limit)
        except NodeOverlayError as exc:
            return self._answer(unavailable, f"overlay not loaded: {exc}")
        except TenantStoreUnavailableError as exc:
            return self._answer(unavailable, f"identity store unavailable: {exc}")
        if not rows:
            return self._answer(
                EnumTenantOutboxFlushOutcome.NOTHING_PENDING, "no unpublished row"
            )
        published = failed = 0
        try:
            for row in rows:
                try:
                    await publisher(row.topic, json.dumps(row.envelope).encode())
                except Exception as exc:
                    failed += 1
                    await store.mark_outbox_failed(row.row_id, type(exc).__name__)
                    continue
                await store.mark_outbox_published(row.row_id)
                published += 1
        except TenantStoreUnavailableError as exc:
            return self._answer(
                unavailable,
                f"identity store unavailable: {exc}",
                published=published,
                failed=failed,
            )
        outcome = (
            EnumTenantOutboxFlushOutcome.FLUSHED
            if failed == 0
            else EnumTenantOutboxFlushOutcome.PARTIAL
        )
        return self._answer(
            outcome,
            f"{published} published, {failed} refused by the bus",
            published=published,
            failed=failed,
        )


class HandlerTenantOutboxFlushOnProvisioned:
    """The provision completed event route: the event carries the provision result, and any completed provision flushes.

    A route takes the model its own producer publishes, so this route does not share the command model of
    ``HandlerTenantOutboxFlush``; it hands the flush a default command and answers what the flush answers.
    """

    def __init__(
        self,
        store: TenantIdentityPostgres | None = None,
        *,
        publisher: Publisher | None = None,
        clock: Callable[[], datetime] = utc_now,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._flush = HandlerTenantOutboxFlush(
            store, publisher=publisher, clock=clock, environ=environ
        )

    async def handle(
        self, request: ModelTenantProvisionResult
    ) -> ModelTenantOutboxFlushResult:
        return await self._flush.handle(
            ModelTenantOutboxFlushCommand(command_id=request.command_id)
        )


__all__ = ["HandlerTenantOutboxFlush", "HandlerTenantOutboxFlushOnProvisioned"]
