# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Hold the newest floor document (OMN-23072, slice 1)."""

from __future__ import annotations

from omnimarket.handlers.floor_document_store import get_floor_document_store
from omnimarket.models.floor_17a.model_floor_document_event import (
    ModelFloorDocumentApplied,
    ModelFloorSimpleDocumentEvent,
)
from omnimarket.protocols.protocol_floor_document_store import (
    ProtocolFloorDocumentStore,
)


class HandlerFloorDocumentReducer:
    """Writes the floor document into the bound store when it is newer than the held one."""

    def __init__(self, store: ProtocolFloorDocumentStore | None = None) -> None:
        self._store = store

    def handle(
        self, request: ModelFloorSimpleDocumentEvent
    ) -> ModelFloorDocumentApplied:
        store = self._store or get_floor_document_store()
        applied = store.put_if_newer(request)
        held = store.latest()
        return ModelFloorDocumentApplied(
            applied=applied,
            computed_at=request.computed_at,
            held_computed_at=held.computed_at if held else None,
            reason="held as the latest document"
            if applied
            else "refused: not newer than the held document",
        )
