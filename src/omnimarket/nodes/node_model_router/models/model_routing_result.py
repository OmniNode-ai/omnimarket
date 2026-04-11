# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelRoutingResult — output from HandlerModelRouter.route_async / route_sync."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelRoutingResult(BaseModel):
    """Result of a routing decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_key: str = Field(..., description="Registry model_id key that was selected.")
    endpoint_url: str = Field(
        ..., description="Resolved base URL for the selected endpoint."
    )
    used_fallback: bool = Field(
        default=False, description="True if the fallback model was used."
    )
    correlation_id: str = Field(..., description="Echoed from the originating request.")


__all__: list[str] = ["ModelRoutingResult"]
