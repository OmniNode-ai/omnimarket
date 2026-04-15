# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Request model for persona lifecycle orchestration."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelPersonaLifecycleRequest(BaseModel):
    """Request for persona lifecycle operations (tick or on-demand)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["on_tick", "on_demand"] = Field(
        ...,
        description="Tick-driven fan-out or single-user on-demand rebuild",
    )
    user_id: str | None = Field(
        default=None,
        description="User ID for on-demand rebuild (required when operation='on_demand')",
    )
