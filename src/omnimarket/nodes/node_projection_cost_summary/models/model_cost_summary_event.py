# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Typed input event for cost summary projection snapshots."""

from __future__ import annotations

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class ModelCostSummaryProjectionEvent(BaseModel):
    """Cost and savings event fields consumed by the cost summary reducer."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    estimated_cost_usd: float = Field(
        default=0.0,
        ge=0.0,
        validation_alias=AliasChoices(
            "estimated_cost_usd",
            "estimatedCostUsd",
            "total_cost_usd",
            "totalCostUsd",
            "cloud_cost_usd",
            "cloudCostUsd",
        ),
    )
    savings_usd: float = Field(
        default=0.0,
        ge=0.0,
        validation_alias=AliasChoices("savings_usd", "savingsUsd"),
    )
    window: str = Field(default="latest")
    timestamp: str | None = Field(default=None)


__all__ = ["ModelCostSummaryProjectionEvent"]
