# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scored capsule record model (OMN-12842 / M2).

Effectiveness is ALWAYS populated from the ROI score event -- a scored capsule
with an empty effectiveness field is a hard validation error here (mirrored by
a DB CHECK constraint in the migration). The raw scored values are immutable;
decay is applied only at read time by the handler / projection read view.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.nodes.node_projection_capsule_store.models.model_capsule_identity import (
    ModelCapsuleIdentity,
)


class ModelCapsuleEffectiveness(BaseModel):
    """Learned effectiveness for a capsule, populated from ROI scores.

    All fields are required and non-null: a capsule that has been scored at
    least once MUST carry these values. ``hit_count >= 1`` because the row
    only exists after at least one scored event.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    success_rate: float = Field(
        ge=0.0,
        le=1.0,
        description="final_success_rate from the ROI score event.",
    )
    first_pass_rate: float = Field(
        ge=0.0,
        le=1.0,
        description="first_pass_rate from the ROI score event.",
    )
    cost_per_success: float = Field(
        ge=0.0,
        description="cost_per_success_usd from the ROI score event.",
    )
    hit_count: int = Field(
        ge=1,
        description="Number of scored events folded into this capsule (>=1).",
    )
    last_scored: datetime = Field(
        description="Timestamp of the most recent scored event (tz-aware UTC)."
    )

    @field_validator("last_scored")
    @classmethod
    def validate_tz_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("last_scored must be timezone-aware")
        return value.astimezone(UTC)


class ModelCapsuleRecord(BaseModel):
    """A durable, scored capsule: identity + effectiveness + validity scope."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    identity: ModelCapsuleIdentity = Field(description="Deterministic identity.")
    effectiveness: ModelCapsuleEffectiveness = Field(
        description="Learned effectiveness (never empty on a scored row)."
    )
    validity_scope: str = Field(
        min_length=1,
        description="Scope the capsule is valid for (e.g. 'repo:omnimarket').",
    )


__all__ = [
    "ModelCapsuleEffectiveness",
    "ModelCapsuleRecord",
]
