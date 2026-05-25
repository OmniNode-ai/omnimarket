# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result model for architecture context assembly."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelDesignPlanContextResult(BaseModel):
    """Structured architecture context output for design-to-plan injection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    architecture_context_block: str = Field(
        description="Formatted ## Architecture Context markdown block.",
    )
    systems_affected: tuple[str, ...] = Field(
        description="Repository or service names identified as affected.",
    )
    decisions_to_honor: tuple[str, ...] = Field(
        description="Architectural decisions the plan must respect.",
    )
    antipatterns_to_avoid: tuple[str, ...] = Field(
        description="Antipatterns the plan must not introduce.",
    )
    impact_summary: tuple[str, ...] = Field(
        description="Dependency impact statements from Memgraph.",
    )


__all__ = ["ModelDesignPlanContextResult"]
