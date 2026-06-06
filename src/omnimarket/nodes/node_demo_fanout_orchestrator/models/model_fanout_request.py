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

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.demo import ModelDemoInferenceResult


class ModelDemoModelConfig(BaseModel):
    """Configuration for a single LLM model to include in the fan-out."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str = Field(
        description="Logical model identifier (e.g. 'qwen3-coder-30b')"
    )
    endpoint_url: str = Field(description="OpenAI-compatible endpoint base URL")
    provider: Literal[
        "openai_compatible",
        "claude_cli",
        "deterministic_fixture",
    ] = Field(
        default="openai_compatible",
        description="Provider adapter selected by the fan-out runtime.",
    )
    api_key_env_var: str | None = Field(
        default=None,
        description="Required environment variable for live OpenAI-compatible calls.",
    )
    max_tokens: int = Field(default=512, ge=1, description="Maximum tokens to generate")
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    timeout_seconds: float = Field(default=30.0, gt=0.0, le=300.0)


class ModelDemoProviderFixture(BaseModel):
    """Deterministic provider fixture used by dry-run fan-out execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outputs: list[str] = Field(
        default_factory=list,
        description="Per-task deterministic outputs. Reused cyclically when shorter than tasks.",
    )
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    latency_ms: float = Field(default=0.0, ge=0.0)


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
    dry_run: bool = Field(
        default=False,
        description="Use deterministic provider fixtures and skip live providers.",
    )
    provider_fixtures: dict[str, ModelDemoProviderFixture] = Field(
        default_factory=dict,
        description="Dry-run fixtures keyed by model_id.",
    )


class ModelDemoFanoutResult(BaseModel):
    """Aggregated output from all model invocations in the fan-out."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    correlation_id: UUID
    results: list[ModelDemoInferenceResult]


__all__ = [
    "ModelDemoFanoutRequest",
    "ModelDemoFanoutResult",
    "ModelDemoInferenceResult",
    "ModelDemoModelConfig",
    "ModelDemoProviderFixture",
]
