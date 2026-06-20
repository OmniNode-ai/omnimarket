# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result model for the capsule-effectiveness feedback edge (OMN-12845 / M5)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelCapsuleFeedbackResult(BaseModel):
    """Outcome of folding one scored runtime row through the feedback edge.

    ``effectiveness_claim_written`` is True ONLY for a controlled-intervention
    row whose effectiveness was written onto the M2 capsule store.
    ``hypothesis_recorded`` is True for an observational row that was routed to
    the hypothesis path instead. Exactly one of the two is True.
    ``capsule_hash`` is populated whenever the row's capsule identity was
    resolved (both paths), so callers can correlate the row to its capsule.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    effectiveness_claim_written: bool = Field(
        description=(
            "True when a controlled-intervention row's effectiveness was written "
            "onto the M2 capsule store as a measured claim."
        ),
    )
    hypothesis_recorded: bool = Field(
        default=False,
        description=(
            "True when an observational row was routed to the hypothesis path "
            "(never written as a measured score)."
        ),
    )
    capsule_hash: str = Field(
        description="Deterministic capsule_hash identity of the row's capsule.",
    )
    rows_upserted: int = Field(
        default=0,
        ge=0,
        description="Number of capsule_store rows upserted (1 for a claim, 0 otherwise).",
    )


__all__ = [
    "ModelCapsuleFeedbackResult",
]
