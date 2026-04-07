# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelTicketClassifyOutput — output from the ticket classify compute node.

Related:
    - OMN-7312: ModelTicketClassification
    - OMN-7579: Migration to omnimarket
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_ticket_classify_compute.models.model_ticket_classification import (
    ModelTicketClassification,
)


class ModelTicketClassifyOutput(BaseModel):
    """Output from the ticket classify compute node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Build loop cycle correlation ID.")
    classifications: tuple[ModelTicketClassification, ...] = Field(
        ..., description="Classification results."
    )
    total_auto_buildable: int = Field(
        default=0, ge=0, description="Count of AUTO_BUILDABLE tickets."
    )
    total_skipped: int = Field(
        default=0,
        ge=0,
        description="Count of SKIP + BLOCKED + NEEDS_ARCH_DECISION tickets.",
    )


__all__: list[str] = ["ModelTicketClassifyOutput"]
