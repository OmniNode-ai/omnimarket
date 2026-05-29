# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Typed input event for token-usage projection snapshots."""

from __future__ import annotations

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class ModelCostTokenUsageProjectionEvent(BaseModel):
    """Token usage fields consumed by the token-usage reducer."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    prompt_tokens: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices("prompt_tokens", "promptTokens"),
    )
    completion_tokens: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices("completion_tokens", "completionTokens"),
    )
    total_tokens: int | None = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices("total_tokens", "totalTokens"),
    )
    window: str = Field(default="latest")
    timestamp: str | None = Field(default=None)


__all__ = ["ModelCostTokenUsageProjectionEvent"]
