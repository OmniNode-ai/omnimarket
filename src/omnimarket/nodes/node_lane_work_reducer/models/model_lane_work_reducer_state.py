# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The folded state the reducer carries (OMN-26706)."""

from __future__ import annotations

from datetime import datetime

from omnibase_core.models.lane_work import ModelLaneHelpTier, ModelLaneWorkChange
from pydantic import BaseModel, ConfigDict, Field


class ModelLaneWorkReducerState(BaseModel):
    """State of one workflow; `name` is the contract state it is in.

    `armed_deadlines` maps a deadline id to the arming it was armed with; a
    deadline fires only with that arming, so a renewed deadline cannot fire from
    its old one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_id: str
    change: ModelLaneWorkChange
    name: str
    seq: int = Field(default=0, ge=0)
    worker_tier: str | None = None
    worker_lane: str | None = None
    expected_head: str | None = None
    proven_head: str | None = None
    handoff_request_id: str | None = None
    handoff_answered_at: datetime | None = None
    help_origin: str | None = None
    help_tiers: tuple[ModelLaneHelpTier, ...] = ()
    help_tier_index: int = Field(default=0, ge=0)
    help_episodes: int = Field(default=0, ge=0)
    repeat_counters: dict[str, int] = Field(default_factory=dict)
    armed_deadlines: dict[str, int] = Field(default_factory=dict)
    deadline_times: dict[str, str] = Field(default_factory=dict)
    arming_counter: int = Field(default=0, ge=0)
    outstanding_actions: dict[str, str] = Field(
        default_factory=dict,
        description="Action id to action name, awaiting completion.",
    )
    seen_input_ids: tuple[str, ...] = ()
    last_refusal: str | None = None


__all__ = ["ModelLaneWorkReducerState"]
