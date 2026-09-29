# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Validated LLM call with an explicit event timestamp."""

from datetime import UTC, datetime

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


class ModelUsageCallEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    call_id: str = Field(
        default="",
        validation_alias=AliasChoices("call_id", "correlation_id", "input_hash"),
    )
    model_name: str = Field(
        min_length=1, validation_alias=AliasChoices("model_name", "model_id")
    )
    tenant_id: str | None = None
    prompt_tokens: int = Field(
        default=0, ge=0, validation_alias=AliasChoices("prompt_tokens", "input_tokens")
    )
    completion_tokens: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices("completion_tokens", "output_tokens"),
    )
    estimated_cost_usd: float = Field(
        default=0.0,
        ge=0,
        allow_inf_nan=False,
        validation_alias=AliasChoices("estimated_cost_usd", "cost_usd"),
    )
    session_id: str | None = None
    timestamp: datetime = Field(
        validation_alias=AliasChoices("timestamp", "emitted_at", "created_at")
    )

    @field_validator("timestamp")
    @classmethod
    def aware_timestamp(cls, value: datetime) -> datetime:
        """Interpret an explicit naive timestamp as UTC, never local time."""
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


__all__ = ["ModelUsageCallEvent"]
