# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelScoredTicket — a ticket with an RSD score.

Related:
    - OMN-7315: node_rsd_fill_compute
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelScoredTicket(BaseModel):
    """A ticket with an RSD (Relative Sprint Difficulty) score."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(..., description="Linear ticket identifier.")
    title: str = Field(..., description="Ticket title.")
    rsd_score: float = Field(
        ..., ge=0.0, description="RSD score (higher = more valuable to fill)."
    )
    priority: int = Field(
        default=0, ge=0, le=4, description="Priority (0=none, 1=urgent, 4=low)."
    )
    labels: tuple[str, ...] = Field(default_factory=tuple, description="Ticket labels.")
    description: str = Field(default="", description="Ticket description.")
    state: str = Field(default="", description="Current ticket state.")


__all__: list[str] = ["ModelScoredTicket"]
