# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one message the fold publishes when a workflow enters help_requested (OMN-26721).

The `ask_help` node action is published on the help topic the contract declares; the help
handler (a later task) subscribes to it, writes the ledger MSG row and sends the one pinned
delegation request. The action id is the key of every write that follows.
"""

from __future__ import annotations

from datetime import datetime

from omnibase_core.models.lane_work import ModelLaneHelpTier
from pydantic import BaseModel, ConfigDict, Field


class ModelLaneWorkHelpMessage(BaseModel):
    """The action id, the tier entry the request is pinned to, and the help request's words."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_id: str = Field(..., min_length=1)
    action_id: str = Field(..., min_length=8, max_length=64)
    state: str = Field(..., min_length=1)
    help_origin: str = Field(..., min_length=1)
    tier: ModelLaneHelpTier
    question: str = Field(..., min_length=1)
    evidence: str = Field(default="no evidence")
    change_ref: str = Field(..., min_length=1)
    requesting_lane: str = Field(..., min_length=1)
    requesting_host: str = Field(..., min_length=1)
    requested_at: datetime
    worker_tier: str | None = None
    no_responder: bool = Field(
        default=False,
        description="True when the tier is the worker's own: no tier is above it, so nothing is sent.",
    )


__all__: list[str] = ["ModelLaneWorkHelpMessage"]
