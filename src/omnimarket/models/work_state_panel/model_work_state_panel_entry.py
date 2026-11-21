# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One panel the floor answers: its identity, the projector it binds and the budget it enforces."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.work_state_projection.model_work_state_projected_event import (
    EnumWorkStateProjectorProfile,
)


class ModelWorkStatePanelEntry(BaseModel):
    """The overlay entry for one panel. Every field is required: an overlay is declared in full."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    panel_id: str = Field(min_length=1)
    projector: EnumWorkStateProjectorProfile
    topic: str
    max_age_s: int = Field(gt=0)
    max_future_skew_s: int = Field(ge=0)
    accept_deadline_s: int = Field(gt=0)
    audience: Literal["team_service"]
    allowed_readers: tuple[str, ...] = Field(min_length=1)
    answer_writer: str = Field(min_length=1)
    reducer_group: str = Field(min_length=1)
    start_position: Literal["latest", "earliest"]


__all__ = ["ModelWorkStatePanelEntry"]
