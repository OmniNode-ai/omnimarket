# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelComparisonResult — output of the model comparison runner effect node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelComparisonCell(BaseModel):
    """Per-model result for a single comparison run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    label: str
    provider: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0
    quality_score: str = ""
    error: str = ""


class ModelComparisonResult(BaseModel):
    """Aggregated result of a side-by-side model comparison run.

    Returned by HandlerModelComparisonRunner after all model calls complete.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_description: str
    comparison_id: str
    cells: tuple[ModelComparisonCell, ...]
    winner_label: str | None
    winner_criteria: str


__all__ = ["ModelComparisonCell", "ModelComparisonResult"]
