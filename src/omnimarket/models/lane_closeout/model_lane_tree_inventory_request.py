# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request for the lane tree inventory."""

from __future__ import annotations

from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class ModelLaneTreeInventoryRequest(BaseModel):
    """The lane directory whose trees are listed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(default_factory=uuid4)
    lane_dir: str = Field(description="The lane's directory on the executing host.")
