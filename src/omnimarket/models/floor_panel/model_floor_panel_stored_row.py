# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The panel row as the store holds it: the projected row and the store's version of it."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.work_state_panel.model_work_state_panel_row import (
    ModelWorkStatePanelRow,
)


class ModelFloorPanelStoredRow(BaseModel):
    """One row as the store returned it: the projected row and the store's monotonic version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row: ModelWorkStatePanelRow
    version: int = Field(ge=0)


__all__ = ["ModelFloorPanelStoredRow"]
