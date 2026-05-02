# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelComparisonRow(BaseModel):
    """A single model's result row in the comparison table."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_key: str
    display_name: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    quality: str = ""
    error: str = ""


class ModelAbCompareResult(BaseModel):
    """Terminal event payload emitted by the AB compare orchestrator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    comparison: list[ModelComparisonRow] = Field(
        default_factory=list,
        description="Per-model rows sorted by cost ascending",
    )
    correlation_id: str = Field(description="Echoed correlation_id for traceability")
    status: str = Field(
        description="COMPLETED or PARTIAL (some models skipped)",
    )
    models_skipped: list[str] = Field(
        default_factory=list,
        description="Model IDs that were skipped (missing key or unreachable)",
    )
