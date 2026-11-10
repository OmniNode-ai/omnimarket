# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory floor document store, the default profile of the floor projection (OMN-23072).

One runtime holds one store: the projection node writes it and the floor route reads it. The
real-backend profile (a database row) binds the same protocol; this one holds the latest document
for the life of the runtime process and the next scheduled tick refills it after a restart.
"""

from __future__ import annotations

import threading

from omnimarket.models.floor_17a.model_floor_document_event import (
    ModelFloorSimpleDocumentEvent,
)
from omnimarket.protocols.protocol_floor_document_store import (
    ProtocolFloorDocumentStore,
)


class InMemoryFloorDocumentStore:
    """Holds the newest document by computed_at; an equal or older one is refused."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held: ModelFloorSimpleDocumentEvent | None = None

    def put_if_newer(self, event: ModelFloorSimpleDocumentEvent) -> bool:
        with self._lock:
            if self._held is not None and event.computed_at <= self._held.computed_at:
                return False
            self._held = event
            return True

    def latest(self) -> ModelFloorSimpleDocumentEvent | None:
        with self._lock:
            return self._held


_STORE: ProtocolFloorDocumentStore = InMemoryFloorDocumentStore()


def get_floor_document_store() -> ProtocolFloorDocumentStore:
    return _STORE


def bind_floor_document_store(store: ProtocolFloorDocumentStore) -> None:
    global _STORE
    _STORE = store


def reset_floor_document_store() -> None:
    bind_floor_document_store(InMemoryFloorDocumentStore())
