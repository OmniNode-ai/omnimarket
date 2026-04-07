# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelRsdFillOutput — output from the RSD fill compute node.

Related:
    - OMN-7315: node_rsd_fill_compute
    - OMN-7578: Migration to omnimarket
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_rsd_fill_compute.models.model_scored_ticket import (
    ModelScoredTicket,
)


class ModelRsdFillOutput(BaseModel):
    """Output from the RSD fill compute node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Build loop cycle correlation ID.")
    selected_tickets: tuple[ModelScoredTicket, ...] = Field(
        ..., description="Top-N tickets selected by RSD score."
    )
    total_candidates: int = Field(..., ge=0, description="Total candidates considered.")
    total_selected: int = Field(..., ge=0, description="Number of tickets selected.")


__all__: list[str] = ["ModelRsdFillOutput"]
