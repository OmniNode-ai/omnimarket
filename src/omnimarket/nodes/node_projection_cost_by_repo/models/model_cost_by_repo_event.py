# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Typed input event for cost-by-repository projection snapshots."""

from __future__ import annotations

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class ModelCostByRepoProjectionEvent(BaseModel):
    """LLM cost event fields consumed by the cost-by-repository reducer."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    repo_name: str = Field(
        default="unknown",
        validation_alias=AliasChoices("repo_name", "repoName", "repository"),
    )
    estimated_cost_usd: float = Field(
        default=0.0,
        ge=0.0,
        validation_alias=AliasChoices(
            "estimated_cost_usd",
            "estimatedCostUsd",
            "total_cost_usd",
            "totalCostUsd",
        ),
    )
    total_tokens: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices("total_tokens", "totalTokens"),
    )
    window: str = Field(default="latest")
    timestamp: str | None = Field(default=None)


__all__ = ["ModelCostByRepoProjectionEvent"]
