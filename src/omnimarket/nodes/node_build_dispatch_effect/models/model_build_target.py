# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelBuildTarget — a single ticket targeted for build dispatch.

Related:
    - OMN-7318: node_build_dispatch_effect
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.enum_buildability import EnumBuildability


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


__all__: list[str] = ["ModelBuildTarget"]
