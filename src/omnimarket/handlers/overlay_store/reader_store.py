# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read one overlay's document from the injected settings store.

The reader holds the store protocol it was constructed with and nothing else: it never chooses a backend, never names
an address and never falls back to a file or a copy on disk. An unreachable store raises what the store raised; an
overlay the store does not hold raises ``OverlayAbsentFromStoreError``.
"""

from __future__ import annotations

from typing import cast

from omnibase_spi.protocols.storage import ProtocolOverlayStore
from pydantic import BaseModel, JsonValue

from omnimarket.handlers.node_overlay_reader import NodeOverlayError


class OverlayAbsentFromStoreError(NodeOverlayError):
    """The store answered, and holds no such overlay."""


class StoreOverlayReader:
    """Reads overlay documents through the store given to its constructor."""

    def __init__(self, store: ProtocolOverlayStore) -> None:
        self._store = store

    async def read_record(self, overlay_id: str) -> dict[str, JsonValue]:
        record = await self._store.read_current(overlay_id)
        if record is None:
            raise OverlayAbsentFromStoreError(
                f"the settings store holds no overlay {overlay_id}"
            )
        return cast("dict[str, JsonValue]", dict(record))

    async def read_document(self, overlay_id: str) -> dict[str, JsonValue]:
        record = await self.read_record(overlay_id)
        document = record.get("document")
        if not isinstance(document, dict):
            raise OverlayAbsentFromStoreError(
                f"the settings store's record of {overlay_id} holds no document"
            )
        return document

    async def read_model[ModelT: BaseModel](
        self, overlay_id: str, model: type[ModelT]
    ) -> ModelT:
        return model.model_validate(await self.read_document(overlay_id))


__all__ = ["OverlayAbsentFromStoreError", "StoreOverlayReader"]
