# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request model for architecture context assembly."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelDesignPlanContextRequest(BaseModel):
    """Inputs for the design-plan context compute node.

    All external query results (Repowise, antipattern registry, Memgraph) are
    injected by the caller. This node owns only the assembly and formatting.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str = Field(description="The design topic or problem statement.")
    repos_mentioned: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Repository names scoping all queries.",
    )
    architectural_decisions: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Pre-resolved Repowise get_why results.",
    )
    antipatterns: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Pre-resolved antipattern registry entries.",
    )
    dependency_impact: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Pre-resolved Memgraph dependency impact statements.",
    )


__all__ = ["ModelDesignPlanContextRequest"]
