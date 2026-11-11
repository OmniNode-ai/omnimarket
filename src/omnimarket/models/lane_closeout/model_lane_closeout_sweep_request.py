# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request for one host-local lane closeout sweep."""

from __future__ import annotations

from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.lane_closeout.model_lane_closeout_sweep_settings import (
    ModelLaneCloseoutSweepSettings,
)


class ModelLaneCloseoutSweepRequest(BaseModel):
    """Close out every ended lane on this host, or only ``lane``; ``read_only`` counts and changes nothing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(default_factory=uuid4)
    registry_root: str = Field(description="Absolute registry root of this host.")
    state_dir: str = Field(
        description="Absolute directory the closeout records are written under."
    )
    host: str
    settings: ModelLaneCloseoutSweepSettings
    now: float = Field(description="Epoch seconds the sweep reads as the present.")
    lane: str | None = Field(default=None, description="Close out only this lane.")
    read_only: bool = False
