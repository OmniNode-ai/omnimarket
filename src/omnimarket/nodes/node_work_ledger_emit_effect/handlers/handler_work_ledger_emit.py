# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Effect handler: publish one ledger row as its typed event (OMN-19513).

The handler types the row (a pure parse) and hands the event to
``node_event_emit_effect``: the local spool outbox, never-drop for
``duty_critical`` topics, drained to the bus by the same path every hook event
uses. This handler holds no Kafka client and no producer code. The constructor
does no I/O; ``handle()`` touches the spool only through the injected emitter.
"""

from __future__ import annotations

from typing import Protocol

from omnibase_core.models.events.work.model_work_ledger_render import ROW_TYPE_BY_KIND

from omnimarket.events.enum_ledger_row_type import EnumLedgerRowType
from omnimarket.events.model_ledger_row_event import (
    ModelLedgerRowEventBase,
    work_ledger_event_id,
    work_ledger_row_id,
)
from omnimarket.models.model_work_ledger_projection_inbound import (
    ModelWorkLedgerProjectionInbound,
)
from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_request import (
    ModelEmitRequest,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_result import (
    ModelEmitResult,
)
from omnimarket.nodes.node_work_ledger_emit_effect.handlers.row_parser import (
    LedgerRowRefusalError,
    parse_ledger_row,
)
from omnimarket.nodes.node_work_ledger_emit_effect.models.model_work_ledger_emit_request import (
    ModelWorkLedgerEmitRequest,
)
from omnimarket.nodes.node_work_ledger_emit_effect.models.model_work_ledger_emit_result import (
    ModelWorkLedgerEmitResult,
)


class ProtocolEventEmitter(Protocol):
    def handle(self, request: ModelEmitRequest) -> ModelEmitResult: ...


class HandlerWorkLedgerEmit:
    """Type one ledger row and emit it through the durable emit-effect spool."""

    def __init__(self, *, emitter: ProtocolEventEmitter | None = None) -> None:
        self._emitter = emitter

    def handle(self, request: ModelWorkLedgerEmitRequest) -> ModelWorkLedgerEmitResult:
        if request.event is not None:
            return self._emit_typed(request)
        try:
            event = parse_ledger_row(
                request.row, ledger_id=request.ledger_id, source=request.source
            )
        except LedgerRowRefusalError as exc:
            return ModelWorkLedgerEmitResult(accepted=False, refusal=str(exc))

        common = {
            "row_id": event.row_id,
            "row_type": event.row_type.value,
            "event_type": event.row_type.event_type,
            "topic": event.row_type.topic,
        }
        try:
            emitted = self.emit_event(event)
        except Exception as exc:  # effect boundary: any spool refusal is a typed result
            return ModelWorkLedgerEmitResult(accepted=False, error=repr(exc), **common)
        return ModelWorkLedgerEmitResult(
            accepted=True, published=emitted.published, **common
        )

    def emit_event(self, event: ModelLedgerRowEventBase) -> ModelEmitResult:
        """Hand one typed event to the emit-effect spool (never a broker directly).

        ``row_id`` is the delivery identity too: a retry of the same row keeps
        the same spool record name, and the fold dedups on it.
        """
        emitter = (
            self._emitter if self._emitter is not None else HandlerEventEmitEffect()
        )
        event_id = str(work_ledger_event_id(event.ledger_id, event.row_id))
        return emitter.handle(
            ModelEmitRequest(
                event_type=event.row_type.event_type,
                payload={**event.model_dump(mode="json"), "event_id": event_id},
                correlation_id=event.row_id,
                event_id=event_id,
            )
        )

    def _emit_typed(
        self, request: ModelWorkLedgerEmitRequest
    ) -> ModelWorkLedgerEmitResult:
        try:
            assert request.event is not None
            row_type = EnumLedgerRowType(ROW_TYPE_BY_KIND[request.event.kind])
            inbound = ModelWorkLedgerProjectionInbound(
                ledger_id=request.ledger_id,
                event_id=request.event.event_id,
                row_id=work_ledger_row_id(request.row),
                raw_row=request.row.strip(),
                source=request.source,
                provenance_kind=request.provenance_kind,
                event=request.event,
            )
            emitter = (
                self._emitter if self._emitter is not None else HandlerEventEmitEffect()
            )
            result = emitter.handle(
                ModelEmitRequest(
                    event_type=row_type.typed_event_type,
                    payload=inbound.model_dump(mode="json"),
                    correlation_id=str(inbound.event_id),
                    event_id=str(inbound.event_id),
                )
            )
            return ModelWorkLedgerEmitResult(
                accepted=True,
                published=result.published,
                row_id=inbound.row_id,
                row_type=row_type.value,
                event_type=row_type.typed_event_type,
                topic=row_type.typed_topic,
            )
        except (ValueError, KeyError) as exc:
            return ModelWorkLedgerEmitResult(accepted=False, refusal=str(exc))


__all__: list[str] = ["HandlerWorkLedgerEmit", "ProtocolEventEmitter"]
