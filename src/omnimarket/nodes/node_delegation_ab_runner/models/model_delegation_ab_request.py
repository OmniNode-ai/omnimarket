# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelDelegationAbRequest -- input for node_delegation_ab_runner."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelDelegationPathConfig(BaseModel):
    """Configuration for one path in the A/B comparison."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(
        ..., description="Human-readable label, e.g. 'baseline' or 'delegated'."
    )
    endpoint_url: str = Field(..., description="Full base URL for the LLM endpoint.")
    model_id: str = Field(..., description="Model identifier passed to the API.")
    api_key: str = Field(
        default="", description="API key; empty for local/unauthenticated endpoints."
    )
    protocol: str = Field(
        default="openai_compatible", description="openai_compatible | anthropic."
    )
    timeout_seconds: float = Field(default=60.0, gt=0)
    is_delegated: bool = Field(
        default=False,
        description="True for the delegated path; False for the frontier baseline.",
    )


class ModelDelegationAbRequest(BaseModel):
    """Command payload requesting a delegation A/B comparison run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_payload: str = Field(
        ..., description="The task prompt to run through both paths."
    )
    system_prompt: str = Field(
        default="", description="Optional system prompt override."
    )
    correlation_id: str = Field(..., description="Unique run ID for event tracing.")
    baseline: ModelDelegationPathConfig = Field(
        ..., description="Frontier model baseline path."
    )
    delegated: ModelDelegationPathConfig = Field(
        ..., description="Delegated/cheaper model path."
    )
    quality_threshold: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Minimum quality score (0-1) for delegated path to pass gate. 0 = gate disabled.",
    )
    pricing_manifest_hash: str = Field(
        default="",
        description="Hash of the pricing manifest used for cost calculation; empty if not available.",
    )


__all__ = ["ModelDelegationAbRequest", "ModelDelegationPathConfig"]
