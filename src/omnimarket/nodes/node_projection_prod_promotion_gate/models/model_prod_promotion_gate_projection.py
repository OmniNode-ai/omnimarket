# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result models for the gate-decision fold (OMN-18999)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_prod_promotion_gate.models.model_prod_promotion_gate_row import (
    ModelProdPromotionGateRow,
)
from omnimarket.nodes.node_projection_prod_promotion_gate.models.model_prod_promotion_gate_wire import (
    ModelProdPromotionGateDecisionWire,
)


class ModelProdPromotionGateProjectionRequest(BaseModel):
    """One decision to fold, plus the delivery facts the fold needs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event: ModelProdPromotionGateDecisionWire = Field(
        ..., description="The gate decision as delivered."
    )
    fallback_correlation_id: str = Field(
        default="",
        description=(
            "The delivery's own deterministic identifier, used as the row key "
            "ONLY when the decision carried no correlation of its own. Passed "
            "in rather than derived here so the fold stays pure and a test can "
            "pin the key it expects."
        ),
    )
    source_topic: str = Field(
        default="", description="Topic the decision was read from."
    )


class ModelProdPromotionGateProjectionResult(BaseModel):
    """What the fold produced."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row: ModelProdPromotionGateRow = Field(
        ..., description="The row this decision becomes."
    )


__all__ = [
    "ModelProdPromotionGateProjectionRequest",
    "ModelProdPromotionGateProjectionResult",
]
