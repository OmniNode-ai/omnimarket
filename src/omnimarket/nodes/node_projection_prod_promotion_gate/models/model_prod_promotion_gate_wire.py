# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gate decision as it arrives off the topic (OMN-18999).

WHY A SEPARATE WIRE MODEL AND NOT ``ModelProdPromotionGateDecision``
    The producing model is ``extra="forbid"`` and its ``outcome`` is REQUIRED.
    Both are right for a producer: a refusal branch added without its own typed
    code must fail to construct rather than borrow a neighbour's token.

    Neither is right for a consumer. This projection is the reader of a topic
    that has been carrying decisions since OMN-13211, and the fields OMN-18999
    added are not on any message already on it. A consumer that required
    ``outcome`` would reject every retained message; one that forbade extras
    would reject every message a LATER producer enriches. Both failures land in
    the dead-letter queue and read as a malformed payload rather than as a
    version skew.

    So this model tolerates in both directions on purpose. That tolerance is
    what "new wire fields land consumer-first" means mechanically: the reader
    ships able to read the old shape AND the new one, and the producer's new
    fields are only ever an improvement to a row that would have been written
    anyway.

WHAT A PRE-OMN-18999 MESSAGE PROJECTS TO
    ``outcome`` absent is not guessed. The fold resolves it from the typed token
    the grant branches have always prefixed onto ``reason`` when one is there,
    and otherwise records ``unknown``. A projected ``unknown`` says "this
    decision predates the typed code", which is a different and honest answer
    from naming a branch that was never asserted.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.runtime_deployment import ModelRedeployDeployContext


class ModelProdPromotionGateDecisionWire(BaseModel):
    """One prod-promotion-gate decision, as delivered."""

    # extra="ignore", NOT "forbid": see the module docstring. A field a later
    # producer adds must not turn a readable decision into a DLQ entry.
    model_config = ConfigDict(frozen=True, extra="ignore")

    allowed: bool = Field(
        ...,
        description=(
            "Whether the promotion may proceed. The ONE field every message on "
            "this topic has always carried, so it is the only required one."
        ),
    )
    reason: str = Field(
        default="",
        description=(
            "The gate's own sentence. Retained on the row verbatim: the typed "
            "outcome says WHICH branch fired, and this says what it measured."
        ),
    )
    outcome: str | None = Field(
        default=None,
        description=(
            "Typed branch code (OMN-18999). Optional here and required on the "
            "producing model -- absent means the decision predates the field."
        ),
    )
    image_digest: str | None = Field(
        default=None,
        description="The digest the gate RESOLVED. None on every refusal.",
    )
    requested_image_digest: str | None = Field(
        default=None,
        description="The digest the promotion ASKED for (OMN-18999 echo).",
    )
    rollback_target: str | None = Field(
        default=None, description="Rollback target carried through the gate."
    )
    grant_id: str | None = Field(
        default=None,
        description=(
            "Authorization grant the decision was evaluated against (OMN-18999 "
            "echo). None is a real answer: it is the fact behind a "
            "missing_promotion_grant refusal, not an unknown."
        ),
    )
    evaluated_at: datetime | None = Field(
        default=None,
        description="Deterministic evaluation time (OMN-18999 echo).",
    )
    correlation_id: UUID | None = Field(
        default=None,
        description=(
            "The redeploy run (OMN-18999 echo). Absent on a pre-OMN-18999 "
            "message, where the writer falls back to the delivery's own "
            "deterministic identifier so the row is still keyed and still "
            "converges on redelivery."
        ),
    )
    deploy_context: ModelRedeployDeployContext | None = Field(
        default=None,
        description=(
            "The echoed deploy request (OMN-16939). The lane and the promotion "
            "batch are read from here rather than restated on the decision."
        ),
    )


__all__ = ["ModelProdPromotionGateDecisionWire"]
