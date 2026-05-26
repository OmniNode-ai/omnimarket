# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for node_demo_fanout_orchestrator [OMN-12235].

Contains:
- ModelDemoModelConfig: configuration for a single LLM model target
- ModelDemoInferenceResult: per-model result with tokens and latency
- ModelDemoFanoutRequest: input to the fan-out orchestrator
- ModelDemoFanoutResult: aggregated output from all model invocations
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.demo_pipeline import ModelDemoInferenceResult


class ModelDemoModelConfig(BaseModel):
    """Configuration for a single LLM model to include in the fan-out."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str = Field(
        description="Logical model identifier (e.g. 'qwen3-coder-30b')"
    )
    endpoint_url: str = Field(description="OpenAI-compatible endpoint base URL")
    max_tokens: int = Field(default=512, ge=1, description="Maximum tokens to generate")
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)


class ModelDemoFanoutRequest(BaseModel):
    """Input to the demo fan-out orchestrator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    correlation_id: UUID
    tasks: list[str] = Field(
        min_length=1, description="One or more prompts to send to each model"
    )
    model_configs: list[ModelDemoModelConfig] = Field(
        min_length=1, description="Models to fan out across"
    )


class ModelDemoFanoutResult(BaseModel):
    """Aggregated output from all model invocations in the fan-out."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    correlation_id: UUID
    results: list[ModelDemoInferenceResult]
