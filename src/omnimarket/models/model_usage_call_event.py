# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Validated LLM call with an explicit event timestamp.

Shared model (OMN-20006): the usage projection node and the local delegation
dispatch port both build it, so it lives in ``omnimarket.models`` rather than in
either node's private models package.
"""

from datetime import UTC, datetime

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from omnimarket.enums.enum_usage_source import EnumUsageSource


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
    # How the call's cost was obtained. A call that does not say is UNKNOWN, never
    # assumed measured: only a measured cost may reach measured_cost_usd.
    usage_source: EnumUsageSource = EnumUsageSource.UNKNOWN
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
