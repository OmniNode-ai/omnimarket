# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelBuildTarget — a single ticket targeted for build dispatch.

Related:
    - OMN-7582: Migrate node_build_dispatch_effect to omnimarket
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumBuildability(StrEnum):
    """Classification of a ticket's buildability by the autonomous loop.

    Values:
        AUTO_BUILDABLE: Ticket can be fully executed by an agent without
            human intervention.
        NEEDS_ARCH_DECISION: Ticket requires architectural decisions or
            design review before implementation can proceed.
        BLOCKED: Ticket has explicit blockers.
        SKIP: Ticket should be skipped in this cycle.
    """

    AUTO_BUILDABLE = "auto_buildable"
    NEEDS_ARCH_DECISION = "needs_arch_decision"
    BLOCKED = "blocked"
    SKIP = "skip"


class ModelBuildTarget(BaseModel):
    """A single ticket targeted for build dispatch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(
        ..., description="Linear ticket identifier."
    )  # pattern-ok: Linear ticket IDs are strings
    title: str = Field(..., description="Ticket title.")
    buildability: EnumBuildability = Field(
        ..., description="Buildability classification."
    )


__all__: list[str] = ["EnumBuildability", "ModelBuildTarget"]
