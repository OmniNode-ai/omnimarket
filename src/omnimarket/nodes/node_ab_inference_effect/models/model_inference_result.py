# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelInferenceResult -- output contract for node_ab_inference_effect."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_usage_source import EnumUsageSource


class ModelInferenceResult(BaseModel):
    """Result of a single AB compare inference call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_key: str = Field(..., description="Registry key identifying the model.")
    prompt_tokens: int = Field(
        default=0, description="Prompt token count from API response.", ge=0
    )
    completion_tokens: int = Field(
        default=0, description="Completion token count from API response.", ge=0
    )
    total_tokens: int = Field(
        default=0, description="Total token count from API response.", ge=0
    )
    latency_ms: int = Field(
        default=0, description="Wall-clock latency in milliseconds.", ge=0
    )
    raw_output: str = Field(
        default="", description="Raw text content from the LLM response."
    )
    error: str = Field(
        default="",
        description="Error message if the call failed; empty string on success.",
    )
    correlation_id: str = Field(
        ..., description="Correlation ID from the parent AB compare run."
    )
    usage_source: EnumUsageSource = Field(
        default=EnumUsageSource.MEASURED,
        description="Whether token counts came from the API response (MEASURED) or are unavailable (UNKNOWN).",
    )


__all__: list[str] = ["ModelInferenceResult"]
