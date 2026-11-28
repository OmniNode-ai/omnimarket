# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory profiles of the floor panel's row reader: the default, and the store-down double."""

from __future__ import annotations

from omnimarket.models.floor_panel.model_floor_panel_stored_row import (
    ModelFloorPanelStoredRow,
)


class InMemoryFloorPanelRowReader:
    """The default profile: the rows put here are the rows read back; nothing reaches a store."""

    def __init__(self) -> None:
        self._rows: dict[str, ModelFloorPanelStoredRow] = {}

    def put(self, key: str, stored: ModelFloorPanelStoredRow) -> None:
        """Hold one stored row under its projector key, the way a test or the local runtime seeds it."""
        self._rows[key] = stored

    async def read(self, key: str) -> ModelFloorPanelStoredRow | None:
        return self._rows.get(key)


class RaisingFloorPanelRowReader:
    """The test double whose read always raises: the store is down."""

    async def read(self, key: str) -> ModelFloorPanelStoredRow | None:
        raise RuntimeError("store down")


__all__ = ["InMemoryFloorPanelRowReader", "RaisingFloorPanelRowReader"]
