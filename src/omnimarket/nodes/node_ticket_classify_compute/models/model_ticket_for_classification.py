# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelTicketForClassification — a single ticket to be classified.

Related:
    - OMN-7312: ModelTicketClassification
    - OMN-7579: Migration to omnimarket
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_ticket_classify_compute.models.model_seam_boundaries import (
    ModelSeamBoundaries,
)


class ModelTicketForClassification(BaseModel):
    """A single ticket to be classified."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(
        ..., description="Linear ticket identifier."
    )  # pattern-ok: Linear ticket IDs are strings
    title: str = Field(..., description="Ticket title.")
    description: str = Field(default="", description="Ticket description/body.")
    labels: tuple[str, ...] = Field(default_factory=tuple, description="Ticket labels.")
    state: str = Field(default="", description="Current ticket state.")
    priority: int = Field(
        default=0, ge=0, le=4, description="Priority (0=none, 1=urgent, 4=low)."
    )
    seam_boundaries: ModelSeamBoundaries | None = Field(
        default=None,
        description="Contract-declared seam boundaries for buildability analysis.",
    )
    contract_yaml: str | None = Field(
        default=None,
        description="Raw contract YAML string; parsed for seam_boundaries if present.",
    )


__all__: list[str] = ["ModelTicketForClassification"]
