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
        api_key_ref: Optional secret reference for authenticated backends.
        extra_headers: Optional extra HTTP headers for the backend (e.g. OpenRouter HTTP-Referer).
        cost_tier: Cost classification (e.g., "low", "medium", "high").
        max_context_tokens: Maximum context window for the selected model.
        max_tokens: Contract-declared per-backend output-token ceiling carried
            from the selected bifrost backend; posted on the inference request.
        system_prompt: System prompt tailored to the task type.
        rationale: Human-readable explanation for the routing decision.
        dod_deterministic: Deterministic quality checks from the task-class contract.
        dod_heuristic: Heuristic quality checks from the task-class contract.
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
    api_key_ref: str | None = Field(
        default=None,
        description="Secret reference for authenticated backends (e.g. OPENROUTER_API_KEY). None for local backends.",
    )
    extra_headers: dict[str, str] | None = Field(
        default=None,
        description="Extra HTTP headers for the backend request (e.g. HTTP-Referer for OpenRouter).",
    )
    cost_tier: str = Field(
        ...,
        description="Cost classification (e.g., 'low', 'medium', 'high').",
    )
    max_context_tokens: int = Field(
        ...,
        description="Maximum context window for the selected model.",
    )
    timeout_ms: int = Field(
        default=30000,
        ge=100,
        le=600000,
        description="Per-backend inference timeout in milliseconds.",
    )
    max_tokens: int = Field(
        ...,
        ge=1,
        description=(
            "OMN-13345: the contract-declared per-backend output-token ceiling "
            "(e.g. 65536 for cloud-glm) resolved from the bifrost backend and "
            "carried onto the routing decision so the orchestrator can post it on "
            "the wire. Without it the orchestrator threads the delegation "
            "request's max_tokens, which is hard-capped at the 8192 "
            "DELEGATION_MAX_TOKENS_HARD_LIMIT default — cloud GLM then truncates "
            "(finish_reason=length) and the quality gate scores low. Fail-closed "
            ">= 1; never silently defaulted (mirrors the OMN-13161 delegation "
            "backend resolution and the OMN-13342 generation fix)."
        ),
    )
    system_prompt: str = Field(
        ...,
        description="System prompt tailored to the task type.",
    )
    rationale: str = Field(
        ...,
        description="Human-readable explanation for the routing decision.",
    )
    dod_deterministic: tuple[str, ...] = Field(
        default=(),
        description=(
            "Deterministic definition-of-done checks from the task-class contract."
        ),
    )
    dod_heuristic: tuple[str, ...] = Field(
        default=(),
        description="Heuristic definition-of-done checks from the task-class contract.",
    )
    tier_name: str = Field(
        default="",
        description=(
            "Name of the routing tier that produced this decision "
            "(e.g. 'local', 'cheap_cloud', 'claude'). "
            "Authoritative escalation handle — do not confuse with cost_tier."
        ),
    )


__all__: list[str] = ["ModelRoutingDecision"]
