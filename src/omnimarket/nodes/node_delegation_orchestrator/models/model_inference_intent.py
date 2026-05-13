# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Inference intent model emitted by the delegation orchestrator."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelInferenceIntent(BaseModel):
    """Intent emitted when the orchestrator requests LLM inference.

    Fields map directly to ModelLlmInferenceRequest:
    - base_url -> base_url
    - model -> model
    - system_prompt -> system_prompt (prepended as system message)
    - prompt -> messages[0] content (user role)
    - max_tokens -> max_tokens
    - temperature -> temperature (varies by task type)
    - operation_type is always CHAT_COMPLETION for delegation tasks
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    intent: str = Field(default="llm_inference")
    base_url: str
    model: str
    system_prompt: str
    prompt: str
    max_tokens: int
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    correlation_id: UUID


__all__: list[str] = ["ModelInferenceIntent"]
