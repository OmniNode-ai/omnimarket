# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelBuildDispatchOutcome — outcome for a single dispatched ticket.

Related:
    - OMN-7318: node_build_dispatch_effect
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelBuildDispatchOutcome(BaseModel):
    """Outcome for a single dispatched ticket."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(
        ..., description="Linear ticket identifier."
    )  # pattern-ok: Linear ticket IDs are strings
    dispatched: bool = Field(..., description="Whether dispatch succeeded.")
    error: str | None = Field(default=None, description="Error if dispatch failed.")


__all__: list[str] = ["ModelBuildDispatchOutcome"]
