# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The decision on one incoming publication, and the row it writes (None writes nothing)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.work_state_panel.enum_panel_decision import EnumPanelDecision
from omnimarket.models.work_state_panel.model_work_state_panel_row import (
    ModelWorkStatePanelRow,
)


class ModelWorkStatePanelDecision(BaseModel):
    """The outcome of the panel's decision, and the row it persists: None when it writes nothing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumPanelDecision
    row: ModelWorkStatePanelRow | None


__all__ = ["ModelWorkStatePanelDecision"]
