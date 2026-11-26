# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Flush the tenant outbox: publish each unpublished envelope to its topic, then stamp it published.

The row is stamped only after the broker acknowledged the record, so a publish that fails leaves the row waiting and the
next flush sends it again (at-least-once; the registry projection upserts by tenant, so a resend is harmless). Until this
node existed nothing moved ``tenant_event_outbox`` to ``onex.tenant.events`` on the dev lane, so a tenant created after the
retired relay stopped never reached ``tenant_registry_mirror``, and every delegation terminal of that tenant failed write
tenant resolution in the delegation projection and left no ``delegation_events`` row.
"""

from __future__ import annotations

import contextlib

from omnimarket.nodes.node_tenant_event_outbox_flush_effect.models.model_tenant_outbox_flush import (
    EnumFlushOutcome,
    ModelTenantOutboxFlushCommand,
    ModelTenantOutboxFlushResult,
)
from omnimarket.nodes.node_tenant_event_outbox_flush_effect.protocols.protocol_tenant_outbox_flush import (
    ProtocolTenantEventPublisher,
    ProtocolTenantOutboxStore,
    TenantOutboxStoreError,
)


class HandlerTenantOutboxFlush:
    """One flush of the outbox over an injected store and publisher."""

    def __init__(
        self,
        store: ProtocolTenantOutboxStore | None = None,
        publisher: ProtocolTenantEventPublisher | None = None,
    ) -> None:
        self._store = store
        self._publisher = publisher

    def _ports(self) -> tuple[ProtocolTenantOutboxStore, ProtocolTenantEventPublisher]:
        if self._store is None or self._publisher is None:
            from omnimarket.nodes.node_tenant_event_outbox_flush_effect.handlers.wiring_overlay import (
                build_ports_from_overlay,
            )

            self._store, self._publisher = build_ports_from_overlay()
        return self._store, self._publisher

    async def handle(
        self, request: ModelTenantOutboxFlushCommand
    ) -> ModelTenantOutboxFlushResult:
        try:
            store, publisher = self._ports()
            rows = await store.pending(request.limit)
        except TenantOutboxStoreError as exc:
            return ModelTenantOutboxFlushResult(
                outcome=EnumFlushOutcome.STORE_UNAVAILABLE, detail=str(exc)
            )
        if not rows:
            return ModelTenantOutboxFlushResult(
                outcome=EnumFlushOutcome.NOTHING_PENDING
            )
        published = failed = 0
        for row in rows:
            key = str(row.envelope.get("correlation_id") or row.id)
            try:
                await publisher.publish(topic=row.topic, key=key, record=row.envelope)
            except Exception as exc:
                failed += 1
                await self._record_failure(store, row.id, type(exc).__name__)
                continue
            try:
                await store.mark_published(row.id)
            except TenantOutboxStoreError:
                failed += 1
                continue
            published += 1
        return ModelTenantOutboxFlushResult(
            outcome=(
                EnumFlushOutcome.PUBLISH_FAILED if failed else EnumFlushOutcome.FLUSHED
            ),
            published=published,
            failed=failed,
        )

    @staticmethod
    async def _record_failure(
        store: ProtocolTenantOutboxStore, row_id: int, error_class: str
    ) -> None:
        with contextlib.suppress(TenantOutboxStoreError):
            await store.mark_failed(row_id, error_class)


__all__ = ["HandlerTenantOutboxFlush"]
