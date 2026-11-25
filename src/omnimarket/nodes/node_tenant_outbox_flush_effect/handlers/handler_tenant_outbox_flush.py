# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Publishes unpublished ``tenant_event_outbox`` rows to the tenant events topic, marking each row only after the ack.

Per row: publish through the injected bus; if the broker acknowledges, mark the row published; if it does not, leave the
row unpublished and record the error, so the next pass sends it again. A row already marked published is never read, so a
replayed command sends nothing twice. A crash between the ack and the mark leaves the row for the next pass: delivery is
at least once, and the envelope's ``correlation_id`` lets the consumer drop the duplicate.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from importlib import resources
from typing import Any

import yaml

from omnimarket.nodes.node_tenant_outbox_flush_effect.handlers.tenant_outbox_postgres import (
    TenantOutboxSchemaError,
)
from omnimarket.nodes.node_tenant_outbox_flush_effect.models.model_tenant_outbox_flush import (
    EnumTenantOutboxFlushOutcome,
    ModelOutboxRow,
    ModelTenantOutboxFlushCommand,
    ModelTenantOutboxFlushResult,
)
from omnimarket.nodes.node_tenant_outbox_flush_effect.protocols.protocol_tenant_outbox_flush import (
    ProtocolTenantEventPublisher,
    ProtocolTenantOutboxStore,
)

_NODE = "node_tenant_outbox_flush_effect"
_MAX_ERROR_CHARS = 200


def utc_now() -> datetime:
    return datetime.now(UTC)


def publish_topic_from_contract() -> str:
    """The topic the flush publishes to, read from this node's contract (``topics.flush.tenant_events``)."""
    text = (
        resources.files(f"omnimarket.nodes.{_NODE}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    contract: dict[str, Any] = yaml.safe_load(text)
    return str(contract["topics"]["flush"]["tenant_events"])


def _message_key(row: ModelOutboxRow) -> str:
    tenant = row.envelope.get("payload", {}).get("tenant", {})
    tenant_id = tenant.get("tenant_id") if isinstance(tenant, dict) else None
    return str(tenant_id) if tenant_id else row.row_id


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS]


class HandlerTenantOutboxFlush:
    """Built bare by the runtime's auto-wiring, and with its store and bus by ``overlay.build_flush``.

    Without a store and a bus a command answers ``FAILED_STORE_UNAVAILABLE``; nothing here opens a connection.
    """

    def __init__(
        self,
        store: ProtocolTenantOutboxStore | None = None,
        publisher: ProtocolTenantEventPublisher | None = None,
        *,
        topic: str | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._publisher = publisher
        self._topic = topic or publish_topic_from_contract()
        self._clock = clock

    async def handle(
        self, request: ModelTenantOutboxFlushCommand
    ) -> ModelTenantOutboxFlushResult:
        now = self._clock()
        store, publisher = self._store, self._publisher
        if store is None or publisher is None:
            return ModelTenantOutboxFlushResult(
                command_id=request.command_id,
                outcome=EnumTenantOutboxFlushOutcome.FAILED_STORE_UNAVAILABLE,
                detail="outbox flush has no store and bus wired",
                emitted_at=now,
            )
        try:
            rows = await store.fetch_unpublished(request.batch_limit)
        except Exception as exc:
            named = f": {exc}" if isinstance(exc, TenantOutboxSchemaError) else ""
            return ModelTenantOutboxFlushResult(
                command_id=request.command_id,
                outcome=EnumTenantOutboxFlushOutcome.FAILED_STORE_UNAVAILABLE,
                detail=f"outbox store unavailable: {type(exc).__name__}{named}",
                emitted_at=now,
            )
        if not rows:
            return ModelTenantOutboxFlushResult(
                command_id=request.command_id,
                outcome=EnumTenantOutboxFlushOutcome.NOTHING_TO_FLUSH,
                detail="no unpublished outbox rows",
                emitted_at=now,
            )
        published = failed = mark_failed = 0
        for row in rows:
            try:
                await publisher.publish(self._topic, _message_key(row), row.envelope)
            except Exception as exc:
                failed += 1
                await self._record_failure(store, row.row_id, exc)
                continue
            try:
                await store.mark_published(row.row_id, self._clock())
            except Exception:
                mark_failed += 1
                continue
            published += 1
        clean = failed == 0 and mark_failed == 0
        return ModelTenantOutboxFlushResult(
            command_id=request.command_id,
            outcome=(
                EnumTenantOutboxFlushOutcome.FLUSHED
                if clean
                else EnumTenantOutboxFlushOutcome.PARTIAL
            ),
            detail=(
                f"published {published}, publish failed {failed}, "
                f"mark failed after ack {mark_failed}"
            ),
            published=published,
            failed=failed,
            mark_failed=mark_failed,
            emitted_at=now,
        )

    async def _record_failure(
        self, store: ProtocolTenantOutboxStore, row_id: str, exc: BaseException
    ) -> None:
        try:
            await store.record_failure(row_id, _error_text(exc))
        except Exception:
            return


__all__ = ["HandlerTenantOutboxFlush", "publish_topic_from_contract", "utc_now"]
