# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the lab host table back from the key-value store (OMN-23983).

The reading is READ with the stored document, or UNKNOWN with the reason. A host list that cannot be read is
never an empty list: no key, a store that does not answer, a value that does not parse, an empty row list and
a document older than the overlay's bound all read UNKNOWN, so a reader that gets nothing says so.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from pydantic import ValidationError

from omnimarket.nodes.node_host_table_keys_ingest_effect.handlers.store_host_keys import (
    ProtocolHostKeyStore,
)
from omnimarket.nodes.node_host_table_keys_ingest_effect.models.model_host_table_keys import (
    ModelHostTableKeysDocument,
    ModelHostTableKeysOverlay,
    ModelHostTableKeysReading,
)
from omnimarket.nodes.node_host_table_keys_read_effect.models.model_host_table_keys_read import (
    ModelHostTableKeysReadRequest,
)


def _unknown(reason: str) -> ModelHostTableKeysReading:
    return ModelHostTableKeysReading(state="UNKNOWN", reason=reason, document=None)


class HandlerHostTableKeysRead:
    """EFFECT handler: the key-value store to a host table reading."""

    def __init__(
        self,
        *,
        store: ProtocolHostKeyStore,
        overlay: ModelHostTableKeysOverlay,
        clock: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._overlay = overlay
        self._clock = clock

    def handle(
        self, request: ModelHostTableKeysReadRequest
    ) -> ModelHostTableKeysReading:
        key = f"{self._overlay.key_prefix}:table"
        try:
            raw = self._store.get(key)
        except Exception as exc:
            return _unknown(
                f"the key-value store did not answer ({type(exc).__name__})"
            )
        if raw is None:
            return _unknown("no host table keys in the key-value store")
        try:
            document = ModelHostTableKeysDocument.model_validate_json(raw)
        except ValidationError:
            return _unknown(f"the value at {key} is malformed")
        age = (self._clock() - document.loaded_at).total_seconds()
        if age > self._overlay.stale_after_seconds:
            return _unknown(
                f"host table keys are stale: loaded {int(age)} s ago, bound {self._overlay.stale_after_seconds} s"
            )
        return ModelHostTableKeysReading(state="READ", reason="", document=document)
