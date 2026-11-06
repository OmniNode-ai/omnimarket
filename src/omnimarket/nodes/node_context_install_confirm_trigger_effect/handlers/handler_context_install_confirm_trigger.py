# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read a session snapshot, assemble the request and confirm installation."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import overload

from omnimarket.models.context_install_confirm_request import (
    ModelInstallConfirmAssemblyRequest,
)
from omnimarket.models.context_install_confirm_trigger import (
    EnumInstallConfirmTriggerOutcome,
    ModelInstallConfirmSkipped,
    ModelInstallConfirmTriggerResult,
    ModelSessionContentAppliedEvent,
)
from omnimarket.nodes.contract_topics import contract_topic
from omnimarket.nodes.node_context_install_confirm_compute.handlers.handler_context_install_confirm import (
    HandlerContextInstallConfirm,
)
from omnimarket.nodes.node_context_install_confirm_request_compute.handlers.handler_context_install_confirm_request import (
    HandlerContextInstallConfirmRequest,
)
from omnimarket.protocols.protocol_session_compact_source import (
    ProtocolSessionCompactSource,
)

_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
_SKIPPED_TOPIC_SUFFIX = "context-install-confirm-skipped.v1"


class HandlerContextInstallConfirmTrigger:
    """Produce a publishable confirmation without I/O beyond the injected reader.

    The reader is the contract's optional ``reader`` dependency
    (``ProtocolSessionCompactSource``): the runtime builds the handler with it when a
    source is registered and with no argument otherwise. The runtime calls
    ``handle(event)`` alone, so a missing source is recorded as a
    ``ModelInstallConfirmSkipped`` event on the contract's skipped topic and never as a
    ``TypeError`` that sends the record to the dead-letter topic.

    The skip is published by the handler through the ``event_publisher`` the runtime's
    wiring injects, to the topic the contract declares, and ``handle`` then returns
    ``None`` so the runtime publishes nothing more. Built without a publisher (a bare
    in-process call), ``handle`` returns the skip model and the contract's
    ``published_events`` route sends it to the same topic.
    """

    def __init__(
        self,
        reader: ProtocolSessionCompactSource | None = None,
        event_publisher: Callable[[str, bytes], None] | None = None,
    ) -> None:
        self._reader = reader
        self._event_publisher = event_publisher

    def publish_skip(self, skipped: ModelInstallConfirmSkipped) -> None:
        """Publish the skip to the contract's skipped topic through the injected publisher."""
        if self._event_publisher is None:
            raise RuntimeError("no event_publisher was injected")
        self._event_publisher(
            contract_topic(_CONTRACT_PATH, "publish_topics", _SKIPPED_TOPIC_SUFFIX),
            skipped.model_dump_json().encode(),
        )

    @overload
    def handle(
        self,
        applied_event: ModelSessionContentAppliedEvent,
        *,
        reader: ProtocolSessionCompactSource,
    ) -> ModelInstallConfirmTriggerResult: ...

    @overload
    def handle(
        self,
        applied_event: ModelSessionContentAppliedEvent,
        *,
        reader: None = None,
    ) -> ModelInstallConfirmTriggerResult | ModelInstallConfirmSkipped | None: ...

    def handle(
        self,
        applied_event: ModelSessionContentAppliedEvent,
        *,
        reader: ProtocolSessionCompactSource | None = None,
    ) -> ModelInstallConfirmTriggerResult | ModelInstallConfirmSkipped | None:
        if applied_event.rows_upserted < 1:
            return ModelInstallConfirmTriggerResult(
                outcome=EnumInstallConfirmTriggerOutcome.NOT_APPLIED
            )
        source = reader if reader is not None else self._reader
        if source is None:
            skipped = ModelInstallConfirmSkipped(event_id=applied_event.event_id)
            if self._event_publisher is None:
                return skipped
            self.publish_skip(skipped)
            return None
        snapshot = source.read(applied_event.event_id)
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
