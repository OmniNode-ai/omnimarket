# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared delegation event payload models."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelDelegationResult(BaseModel):
    """Delegation outcome: content, quality status, model info, and metrics."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    correlation_id: UUID = Field(
        ...,
        description="Tracks this result back to the original request.",
    )
    task_type: str = Field(
        ...,
        description="The task classification from the original request.",
    )
    model_used: str = Field(
        ...,
        description="Name of the LLM model that produced the response.",
    )
    endpoint_url: str = Field(
        ...,
        description="URL of the LLM endpoint used.",
    )
    content: str = Field(
        ...,
        description="The LLM-generated response content.",
    )
    quality_passed: bool = Field(
        ...,
        description="Whether the quality gate accepted the response.",
    )
    quality_score: float = Field(
        ...,
        description="Quality score from 0.0 to 1.0.",
    )
    latency_ms: int = Field(
        ...,
        description="End-to-end latency in milliseconds.",
    )
    prompt_tokens: int = Field(
        default=0,
        description="Number of tokens in the prompt.",
    )
    completion_tokens: int = Field(
        default=0,
        description="Number of tokens in the completion.",
    )
    total_tokens: int = Field(
        default=0,
        description="Total tokens used (prompt + completion).",
    )
    fallback_to_claude: bool = Field(
        ...,
        description="Whether fallback to Claude was triggered.",
    )
    failure_reason: str = Field(
        default="",
        description="Reason for failure, empty string if successful.",
    )
    tokens_to_compliance: int = Field(
        default=0,
        ge=0,
        description="Total tokens across all compliance attempts.",
    )
    compliance_attempts: int = Field(
        default=1,
        ge=1,
        description="Number of LLM invocations to reach compliance.",
    )


__all__ = ["ModelDelegationResult"]
