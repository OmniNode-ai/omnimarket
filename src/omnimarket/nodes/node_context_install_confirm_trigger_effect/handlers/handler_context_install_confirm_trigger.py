# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read a session snapshot, assemble the request and confirm installation."""

from __future__ import annotations

from omnimarket.models.context_install_confirm_request import (
    ModelInstallConfirmAssemblyRequest,
)
from omnimarket.models.context_install_confirm_trigger import (
    EnumInstallConfirmSkipReason,
    EnumInstallConfirmTriggerOutcome,
    ModelInstallConfirmSkipped,
    ModelInstallConfirmTriggerResult,
    ModelSessionContentAppliedEvent,
)
from omnimarket.nodes.node_context_install_confirm_compute.handlers.handler_context_install_confirm import (
    HandlerContextInstallConfirm,
)
from omnimarket.nodes.node_context_install_confirm_request_compute.handlers.handler_context_install_confirm_request import (
    HandlerContextInstallConfirmRequest,
)
from omnimarket.protocols.protocol_session_compact_source import (
    ProtocolSessionCompactSource,
)


class HandlerContextInstallConfirmTrigger:
    """Produce a publishable confirmation without I/O beyond the injected reader.

    The reader is the contract's optional ``reader`` dependency, given at construction.
    The runtime builds the handler from the contract; while it supplies no reader the
    handler answers every applied event with a typed skip and never raises.
    """

    def __init__(self, reader: ProtocolSessionCompactSource | None = None) -> None:
        self._reader = reader

    def handle(
        self, applied_event: ModelSessionContentAppliedEvent
    ) -> ModelInstallConfirmTriggerResult | ModelInstallConfirmSkipped:
        reader = self._reader
        if reader is None:
            return ModelInstallConfirmSkipped(
                event_id=applied_event.event_id,
                rows_upserted=applied_event.rows_upserted,
                reason=EnumInstallConfirmSkipReason.READER_NOT_WIRED,
            )
        if applied_event.rows_upserted < 1:
            return ModelInstallConfirmTriggerResult(
                outcome=EnumInstallConfirmTriggerOutcome.NOT_APPLIED
            )
        snapshot = reader.read(applied_event.event_id)
        if snapshot is None:
            return ModelInstallConfirmTriggerResult(
                outcome=EnumInstallConfirmTriggerOutcome.SESSION_UNRESOLVED
            )
        if not snapshot.chunks:
            return ModelInstallConfirmTriggerResult(
                outcome=EnumInstallConfirmTriggerOutcome.NO_COMPACT_RECORDS
            )

        assembled = HandlerContextInstallConfirmRequest().handle(
            ModelInstallConfirmAssemblyRequest(
                session_id=snapshot.session_id,
                chunks=snapshot.chunks,
                activations=snapshot.activations,
                newest_captured_at=max(chunk.emitted_at for chunk in snapshot.chunks),
            )
        )
        if assembled.packages_unreadable and assembled.packages_read == 0:
            return ModelInstallConfirmTriggerResult(
                outcome=EnumInstallConfirmTriggerOutcome.ALL_PACKAGES_UNREADABLE,
                packages_unreadable=assembled.packages_unreadable,
            )

        request = assembled.request
        return ModelInstallConfirmTriggerResult(
            outcome=EnumInstallConfirmTriggerOutcome.REQUEST_READY,
            request=request,
            confirmation=HandlerContextInstallConfirm().handle(request)
            if request is not None
            else None,
            packages_unreadable=assembled.packages_unreadable,
        )


__all__ = ["HandlerContextInstallConfirmTrigger"]
