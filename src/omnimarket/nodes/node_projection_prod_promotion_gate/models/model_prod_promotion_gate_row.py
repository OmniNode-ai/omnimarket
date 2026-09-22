# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The durable row one prod-promotion-gate evaluation becomes (OMN-18999)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

#: What a decision minted before OMN-18999 projects to when its ``reason``
#: carries no typed token either. Named rather than spelled inline so a query
#: for "decisions we cannot classify" is a constant comparison, and so the
#: value can never be confused with a branch that was actually asserted.
UNKNOWN_OUTCOME = "unknown"


class ModelProdPromotionGateRow(BaseModel):
    """One gate evaluation, ready to write.

    The four fields the acceptance criterion names -- the typed reason, the
    grant identifier, the requested digest and the evaluation time -- are
    ``outcome``, ``grant_id``, ``requested_image_digest`` and ``evaluated_at``.
    They are on this model for the same reason they are on the relation: a
    refusal that cannot say what was refused, under whose authorization, and
    when, answers none of the questions the row exists to answer.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(
        ...,
        description=(
            "The redeploy run, and the row's key. Falls back to the delivery's "
            "deterministic identifier when the decision carried none."
        ),
    )
    outcome: str = Field(
        ...,
        min_length=1,
        description=(
            "Typed code for the branch that refused or allowed. UNKNOWN_OUTCOME "
            "only for a decision that predates the typed code and carried no "
            "token in its reason."
        ),
    )
    allowed: bool = Field(
        ...,
        description=(
            "Stored beside the outcome rather than derived from it at read "
            "time. An allow and a never-ran are the same absence without a "
            "row; with one, they are a boolean apart."
        ),
    )
    reason: str = Field(
        default="",
        description="The gate's own sentence, verbatim.",
    )
    grant_id: str | None = Field(
        default=None,
        description="Authorization grant the decision was evaluated against.",
    )
    requested_image_digest: str | None = Field(
        default=None, description="The digest the promotion asked for."
    )
    resolved_image_digest: str | None = Field(
        default=None,
        description=(
            "The digest the gate resolved. Distinct column from the requested "
            "one: on every refusal this is NULL and that one is the answer to "
            "'what was being promoted'."
        ),
    )
    rollback_target: str | None = Field(
        default=None, description="Rollback target carried through the gate."
    )
    runtime_lane: str | None = Field(
        default=None,
        description="Lane from the echoed deploy context; None when unechoed.",
    )
    promotion_batch_id: str | None = Field(
        default=None,
        description="Promotion batch from the echoed deploy context.",
    )
    evaluated_at: datetime | None = Field(
        default=None,
        description=(
            "The deterministic evaluation time. Event time, never an ingest "
            "clock -- a replay reproduces this row rather than re-dating it."
        ),
    )
    source_topic: str = Field(
        default="",
        description="Topic the decision was read from, for provenance.",
    )


__all__ = ["UNKNOWN_OUTCOME", "ModelProdPromotionGateRow"]
