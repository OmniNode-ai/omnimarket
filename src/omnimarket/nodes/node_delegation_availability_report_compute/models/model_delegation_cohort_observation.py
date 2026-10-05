# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure single-attempt cohort reporting for availability and content (OMN-18931)."""

from __future__ import annotations

from typing import Self
from uuid import UUID

from omnibase_core.models.delegation.wire import ModelDelegationResult
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.cost.usage_normalizer import ModelUsageResult
from omnimarket.enums.enum_usage_source import EnumUsageSource


class ModelDelegationCohortObservation(BaseModel):
    """One fixed-cohort request, including explicit absence of a terminal.

    Usage and its source hash are supplied from retained evidence, never inferred
    from a zero token count. The caller selects a fixed matched cohort; this
    reporter neither dispatches nor retries a request.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    backend_tier: str = Field(min_length=1)
    attempts_count: int = Field(ge=0)
    terminal: ModelDelegationResult | None
    usage: ModelUsageResult
    source_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        if self.terminal is not None:
            if self.terminal.correlation_id != self.correlation_id:
                raise ValueError("terminal must belong to the observed request")
            if self.terminal.attempts_count != self.attempts_count:
                raise ValueError("attempts_count must match the terminal")
            if (
                self.terminal.cost_tier_name
                and self.terminal.cost_tier_name != self.backend_tier
            ):
                raise ValueError("backend_tier must match the terminal route tier")
            if self.terminal.operational_outcome is None:
                raise ValueError("cohort reporting requires typed terminal outcomes")
        if self.usage.usage_source is EnumUsageSource.ESTIMATED:
            if not self.usage.estimation_method:
                raise ValueError("estimated usage requires estimation_method")
        elif self.usage.estimation_method is not None:
            raise ValueError("only estimated usage may carry estimation_method")
        return self
