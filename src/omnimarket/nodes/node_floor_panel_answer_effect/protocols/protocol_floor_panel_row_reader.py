# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The port the floor panel answers read their held rows through, and read-only."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.models.floor_panel.model_floor_panel_stored_row import (
    ModelFloorPanelStoredRow,
)


@runtime_checkable
class ProtocolFloorPanelRowReader(Protocol):
    """One read of the row a panel's projector holds; nothing here writes."""

    async def read(self, key: str) -> ModelFloorPanelStoredRow | None: ...


__all__ = ["ProtocolFloorPanelRowReader"]
