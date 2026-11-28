# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reducer's configuration: inputs it receives, never reads (OMN-26706)."""

from __future__ import annotations

from omnibase_core.models.lane_work import ModelLaneHelpTier
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_DEADLINE_SECONDS: dict[str, int] = {
    "worker": 900,
    "claim_read": 120,
    "observation": 600,
    "poll": 300,
    "wait_budget": 900,
    "proof": 1800,
    "post": 120,
    "handoff": 300,
    "confirmation": 600,
    "help": 900,
}


class ModelLaneWorkReducerConfig(BaseModel):
    """Repeat bound, deadlines, wait budget, help budget and help chain.

    The orchestrator reads them from its resolved overlay (the settings store
    first) and passes them in; the chain is copied into a workflow's state when
    its start folds.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    repeat_bound: int = Field(default=2, ge=1, description="Same failure twice.")
    help_budget: int = Field(default=3, ge=0, description="Help episodes per workflow.")
    deadline_seconds: dict[str, int] = Field(
        default_factory=lambda: dict(DEFAULT_DEADLINE_SECONDS),
        description="Seconds each named deadline is armed for; wait_budget is the wait budget.",
    )
    help_tiers: tuple[ModelLaneHelpTier, ...] = ()


__all__ = ["DEFAULT_DEADLINE_SECONDS", "ModelLaneWorkReducerConfig"]
