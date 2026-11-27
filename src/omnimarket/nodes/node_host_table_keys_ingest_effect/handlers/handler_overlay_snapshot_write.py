# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Apply every overlay's committed snapshot record to the injected settings store (Task 20, OMN-24028).

The store arrives through the constructor as the protocol; this module names no implementation, address or prefix. The
store applies a record only when its revision is newer than the one it holds, or the same with a later heartbeat, and
counts a record that carries no revision, so a replay of the compacted topic never moves a value back.
"""

from __future__ import annotations

import os

from omnibase_spi.protocols.storage import ProtocolOverlayStore

from omnimarket.handlers.overlay_store.store_wiring import build_overlay_store
from omnimarket.models.overlay_store.model_overlay_settings_record import (
    ModelOverlaySettingsRecord,
)


class HandlerOverlaySnapshotWrite:
    """EFFECT handler: one snapshot topic record to the settings store."""

    def __init__(self, *, store: ProtocolOverlayStore | None = None) -> None:
        # Given nothing (the runtime's auto-wiring builds the handler bare), the store is built from the bootstrap
        # inputs in the environment on the first record, so a runtime with no store configured wires and refuses then.
        self._store = store

    def _resolve(self) -> ProtocolOverlayStore:
        if self._store is None:
            self._store = build_overlay_store(os.environ)
        return self._store

    async def handle(self, request: ModelOverlaySettingsRecord) -> None:
        """Write the record; the store decides whether it supersedes what it holds."""
        await self._resolve().write_version(request.model_dump(mode="json"))
