# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The answer the floor panel gives one request: the held state, or the named refusal of the whole request."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from omnimarket.models.floor_panel.enum_floor_panel_code import EnumFloorPanelCode
from omnimarket.models.floor_panel.model_floor_panel_refusal_view import (
    ModelFloorPanelRefusalView,
)
from omnimarket.models.work_state_panel.enum_panel_state import EnumPanelState
from omnimarket.models.work_state_panel.model_work_state_panel_body import (
    ModelWorkStatePanelBody,
)


class ModelFloorPanelAnswer(BaseModel):
    """The answer to one panel request: the panel's state and body when answered, or the refusal when not."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    panel: str | None
    state: EnumPanelState | None = None
    as_of: datetime | None = None
    age_s: int | None = None
    max_age_s: int | None = None
    epoch: int | None = None
    row_version: int | None = None
    publication_id: str | None = None
    reason: str | None = None
    refusal: ModelFloorPanelRefusalView | None = None
    body: ModelWorkStatePanelBody | None = None
    code: EnumFloorPanelCode | None = None


__all__ = ["ModelFloorPanelAnswer"]
