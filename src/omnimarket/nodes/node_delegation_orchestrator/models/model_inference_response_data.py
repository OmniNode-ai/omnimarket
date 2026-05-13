# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Inference response data model for the delegation orchestrator.

Captures the relevant fields from an LLM inference response without
requiring the full ModelLlmInferenceResponse (which has complex invariants).
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelInferenceResponseData(BaseModel):
    """Captures the relevant fields from an LLM inference response."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Workflow correlation ID.")
    content: str = Field(..., description="Generated text from the LLM.")
    model_used: str = Field(
        ..., description="Model identifier that produced the response."
    )
    llm_call_id: str = Field(
        default="",
        description="Upstream LLM call ID for cost reconciliation (e.g. OpenAI id field).",
    )
    latency_ms: int = Field(default=0, description="Inference latency in milliseconds.")
    prompt_tokens: int = Field(default=0, description="Prompt token count.")
    completion_tokens: int = Field(default=0, description="Completion token count.")
    total_tokens: int = Field(default=0, description="Total token count.")


__all__: list[str] = ["ModelInferenceResponseData"]
