# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelRoutingRequest — input to HandlerModelRouter.route_async / route_sync."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelRoutingRequest(BaseModel):
    """Request to route to a model endpoint per the handler's ModelRoutingPolicy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str = Field(..., description="Prompt text to route to a model.")
    role: str = Field(
        ..., description="Caller role (used for fallback authorization check)."
    )
    correlation_id: str = Field(
        ..., description="Trace/correlation ID for the originating call."
    )


__all__: list[str] = ["ModelRoutingRequest"]
