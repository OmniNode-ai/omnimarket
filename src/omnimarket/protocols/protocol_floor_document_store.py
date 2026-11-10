# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the projection holds the latest floor document (OMN-23072)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.models.floor_17a.model_floor_document_event import (
    ModelFloorSimpleDocumentEvent,
)


@runtime_checkable
class ProtocolFloorDocumentStore(Protocol):
    """Latest-document store: the projection writes it, the runtime route reads it."""

    def put_if_newer(self, event: ModelFloorSimpleDocumentEvent) -> bool:
        """Hold the event when it is newer than the held one; False when it is not."""
        ...

    def latest(self) -> ModelFloorSimpleDocumentEvent | None:
        """The held event, or None before any document has been projected."""
        ...
