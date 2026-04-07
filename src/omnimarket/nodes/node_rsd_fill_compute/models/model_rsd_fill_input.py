# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelRsdFillInput — input to the RSD fill compute node.

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


class ModelRsdFillInput(BaseModel):
    """Input to the RSD fill compute node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Build loop cycle correlation ID.")
    scored_tickets: tuple[ModelScoredTicket, ...] = Field(
        ..., description="All scored tickets available for selection."
    )
    max_tickets: int = Field(
        default=5, ge=1, le=20, description="Maximum tickets to select."
    )


__all__: list[str] = ["ModelRsdFillInput"]
