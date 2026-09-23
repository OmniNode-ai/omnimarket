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


class ModelProdPromotionGateProjectionRequest(ModelProdPromotionGateDecisionWire):
    """One gate decision, exactly as the runtime adapter hands it over.

    THIS IS THE DECISION, NOT A WRAPPER AROUND IT (OMN-19240). The shared
    ``runtime_local_adapter`` builds a def-B handler's input with
    ``input_model_cls(**payload_dict)`` over the unwrapped DOMAIN payload, so a
    model declaring ``{event, ...}`` can never be constructed from a bus
    message: the payload has no ``event`` key, and the adapter has no wrapper
    to put one in.

    The previous revision of this model was that wrapper, with
    ``extra="forbid"``. It validated in every writer test, because the writer
    built it by hand, and failed on every live decision with ``11 validation
    errors`` -- ``event`` missing and each of the decision's ten fields an
    extra input. The combined dispatch returned ``handler_error``, every
    decision was dead-lettered, and the DLQ replay loop fed one of them back
    about 22,900 times. ``ModelLabLaneHealthRequest`` records the identical
    defect and fix for its own fold (OMN-18769).

    Inheriting the wire model keeps its tolerance in both directions: an
    older decision without the OMN-18999 fields still validates, and a key a
    later producer or the transport adds is ignored rather than refused.
    ``allowed``, the one field every decision has always carried, stays
    required, so a payload that is not a gate decision is still refused here.

    The two delivery facts below are never on the bus payload. The writer,
    which knows the message's coordinates, supplies them; the adapter path
    supplies neither and the fold derives a key from the decision itself.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    fallback_correlation_id: str = Field(
        default="",
        description=(
            "The delivery's own deterministic identifier, used as the row key "
            "ONLY when the decision carried no correlation of its own. Passed "
            "in rather than derived here so the fold stays pure and a test can "
            "pin the key it expects. Empty on the adapter path."
        ),
    )
    source_topic: str = Field(
        default="", description="Topic the decision was read from."
    )

    def decision(self) -> ModelProdPromotionGateDecisionWire:
        """The decision alone, without the delivery facts."""
        return ModelProdPromotionGateDecisionWire.model_validate(
            self.model_dump(
                include=set(ModelProdPromotionGateDecisionWire.model_fields)
            )
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
