# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Delegation routing decision DTO shared outside the routing reducer node."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.enums.enum_dod_band_source import EnumDodBandSource
from omnimarket.enums.enum_requested_response_shape import (
    EnumRequestedResponseShape,
)


class ModelRoutingDecision(BaseModel):
    """Routing reducer output: selected model, endpoint, cost tier, and rationale."""

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
        description=(
            "Secret reference for authenticated backends. None for local backends."
        ),
    )
    extra_headers: dict[str, str] | None = Field(
        default=None,
        description="Extra HTTP headers for the backend request.",
    )
    cost_tier: str = Field(
        ...,
        description="Cost classification.",
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
        description="Contract-declared per-backend output-token ceiling.",
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
        description="Deterministic definition-of-done checks.",
    )
    dod_heuristic: tuple[str, ...] = Field(
        default=(),
        description="Heuristic definition-of-done checks.",
    )
    requested_shape: EnumRequestedResponseShape = Field(
        default=EnumRequestedResponseShape.UNCONSTRAINED,
        description="Response shape the caller's prompt declared.",
    )
    dod_deterministic_source: EnumDodBandSource = Field(
        default=EnumDodBandSource.CLASS_DEFINITION_OF_DONE,
        description="Contract key that supplied dod_deterministic.",
    )
    dod_heuristic_source: EnumDodBandSource = Field(
        default=EnumDodBandSource.CLASS_DEFINITION_OF_DONE,
        description="Contract key that supplied dod_heuristic.",
    )
    tier_name: str = Field(
        default="",
        description="Name of the routing tier that produced this decision.",
    )
    selected_backend_ref: str = Field(
        default="",
        description="Raw backend_ref from routing_tiers.yaml.",
    )
    route: str | None = Field(
        default=None,
        description=(
            "Declared backend route selected for this inference attempt. Paired "
            "with provider; None is the legacy/unproven shape."
        ),
    )
    provider: str | None = Field(
        default=None,
        description=(
            "Declared provider identity for route. Paired with route; None is "
            "the legacy/unproven shape."
        ),
    )

    @model_validator(mode="after")
    def _validate_provenance_pair(self) -> ModelRoutingDecision:
        """Keep factual execution provenance either complete or absent."""
        if (self.route is None) != (self.provider is None):
            raise ValueError("route and provider must both be set or both be None")
        if self.route is not None:
            assert self.provider is not None
            if not self.route.strip() or not self.provider.strip():
                raise ValueError("route and provider must be nonblank when set")
        return self


__all__: list[str] = ["ModelRoutingDecision"]
