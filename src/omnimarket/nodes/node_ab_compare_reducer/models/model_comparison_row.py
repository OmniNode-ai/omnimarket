# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelComparisonRow -- one row in the AB compare result table."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelComparisonRow(BaseModel):
    """One model's results in the side-by-side comparison."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_key: str = Field(..., description="Registry key identifying the model.")
    display_name: str = Field(..., description="Human-readable model name.")
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0.0, description="Calculated cost in USD.")
    latency_ms: int = Field(default=0, ge=0)
    quality: str = Field(
        default="", description="Quality check result: pass | fail | skipped."
    )
    error: str = Field(
        default="", description="Error message if the inference call failed."
    )


__all__: list[str] = ["ModelComparisonRow"]
