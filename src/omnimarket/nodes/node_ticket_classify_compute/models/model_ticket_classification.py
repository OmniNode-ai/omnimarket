# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelTicketClassification — classification result for a single ticket.

Related:
    - OMN-7312: ModelTicketClassification
    - OMN-7579: Migration to omnimarket
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_ticket_classify_compute.models.enum_buildability import (
    EnumBuildability,
)


class ModelTicketClassification(BaseModel):
    """Classification result for a single ticket.

    Produced by the ticket classify compute node using keyword heuristics.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(
        ..., description="Linear ticket identifier (e.g. OMN-1234)."
    )  # pattern-ok: Linear ticket IDs are strings
    title: str = Field(..., description="Ticket title.")
    buildability: EnumBuildability = Field(
        ..., description="Buildability classification."
    )
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Classification confidence score."
    )
    matched_keywords: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Keywords that contributed to the classification.",
    )
    reason: str = Field(
        default="", description="Human-readable classification rationale."
    )


__all__: list[str] = ["ModelTicketClassification"]
