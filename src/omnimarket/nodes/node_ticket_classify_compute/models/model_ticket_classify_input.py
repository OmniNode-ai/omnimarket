# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelTicketClassifyInput — input to the ticket classify compute node.

Related:
    - OMN-7312: ModelTicketClassification
    - OMN-7579: Migration to omnimarket
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_ticket_classify_compute.models.model_ticket_for_classification import (
    ModelTicketForClassification,
)


class ModelTicketClassifyInput(BaseModel):
    """Input to the ticket classify compute node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Build loop cycle correlation ID.")
    tickets: tuple[ModelTicketForClassification, ...] = Field(
        ..., description="Tickets to classify."
    )


__all__: list[str] = ["ModelTicketClassifyInput"]
