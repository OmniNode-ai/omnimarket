# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Routing decision model for the delegation pipeline.

Represents the output of the routing reducer: which model, endpoint,
cost tier, and system prompt to use for a given delegation request.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelRoutingDecision(BaseModel):
    """Routing output: selected model, endpoint, cost tier, and rationale.

    Attributes:
        correlation_id: Tracks this decision back to the original request.
        task_type: The task classification from the original request.
        selected_model: Name of the LLM model selected for this task.
        selected_backend_id: Identifier for the backend in the Bifrost config.
        endpoint_url: URL of the selected LLM endpoint.
        cost_tier: Cost classification (e.g., "low", "medium", "high").
        max_context_tokens: Maximum context window for the selected model.
        system_prompt: System prompt tailored to the task type.
        rationale: Human-readable explanation for the routing decision.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    correlation_id: UUID = Field(
        ...,
        description="Tracks this decision back to the original request.",
    )
    task_type: str = Field(
        ...,
        description="The task classification from the original request.",
    )
    selected_model: str = Field(
        ...,
        description="Name of the LLM model selected for this task.",
    )
    selected_backend_id: UUID = Field(
        ...,
        description="Identifier for the backend in the Bifrost config.",
    )
    endpoint_url: str = Field(
        ...,
        description="URL of the selected LLM endpoint.",
    )
    cost_tier: str = Field(
        ...,
        description="Cost classification (e.g., 'low', 'medium', 'high').",
    )
    max_context_tokens: int = Field(
        ...,
        description="Maximum context window for the selected model.",
    )
    system_prompt: str = Field(
        ...,
        description="System prompt tailored to the task type.",
    )
    rationale: str = Field(
        ...,
        description="Human-readable explanation for the routing decision.",
    )


__all__: list[str] = ["ModelRoutingDecision"]
