# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared capsule-feedback event models (OMN-12845 / M5).

The M5 closed loop feeds a scored runtime ROI row back onto the durable M2
capsule store. The feedback edge is the consume-side reducer that performs that
write, but the row it consumes and the attribution-honesty vocabulary it
enforces are shared types (the runner produces them, the feedback edge consumes
them, the OCC evidence bundle replays them), so they live in
:mod:`omnimarket.events` rather than inside one node's private models package.

Attribution honesty (BAC plan theme-5, lines 119-121):

* CONTROLLED_INTERVENTION -- the row came from a randomized-arm-order,
  fixed-model/temp experiment trial; it may carry an effectiveness CLAIM that is
  written onto a capsule.
* OBSERVATIONAL -- the row came from a non-controlled session log; it may only
  generate a HYPOTHESIS and is NEVER written onto a capsule as a measured score.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.enums.enum_proof_class import EnumProofClass


class EnumRowAttributionClass(StrEnum):
    """How a scored runtime row may be attributed to capsule effectiveness.

    controlled_intervention: randomized arm order, fixed model/temp -- the row
        is a controlled measurement and may carry an effectiveness CLAIM.
    observational: a non-controlled session log -- the row may only generate a
        HYPOTHESIS; an effectiveness claim from this row is forbidden.
    """

    CONTROLLED_INTERVENTION = "controlled_intervention"
    OBSERVATIONAL = "observational"


class ModelScoredRuntimeRow(BaseModel):
    """A scored runtime ROI row presented to the capsule feedback edge.

    Carries the M2 capsule provenance (the identity-bearing fields), the ROI
    effectiveness numbers the scorer computed, the proof class (must be
    runtime-observed), the attribution class (controlled vs observational), and
    the routing-source provenance so a winning factor is not a routing artifact.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # --- Capsule identity provenance (M2 stable-identity fields) ---
    factor: EnumContextFactor = Field(description="Context factor category.")
    content: str = Field(min_length=1, description="Capsule content body.")
    source_artifact: str = Field(
        min_length=1, description="Source artifact path/reference."
    )
    source_commit: str = Field(
        min_length=1, description="Source commit the capsule was captured from."
    )
    validity_scope: str = Field(
        min_length=1,
        description="Scope the capsule is valid for (e.g. 'repo:omnimarket').",
    )

    # --- Scored effectiveness numbers ---
    final_success_rate: float = Field(
        ge=0.0, le=1.0, description="Maps to stored success_rate."
    )
    first_pass_rate: float = Field(
        ge=0.0, le=1.0, description="Maps to stored first_pass_rate."
    )
    cost_per_success_usd: float = Field(
        ge=0.0, description="Maps to stored cost_per_success."
    )

    # --- Provenance / honesty fields ---
    proof_class: EnumProofClass = Field(
        description=(
            "Provenance class of the row; a capsule effectiveness claim is only "
            "ever written from a runtime-observed row."
        ),
    )
    attribution_class: EnumRowAttributionClass = Field(
        description=(
            "controlled_intervention rows may claim effectiveness; observational "
            "rows may only generate a hypothesis."
        ),
    )
    routing_source: str = Field(
        min_length=1,
        description=(
            "Provenance of the model/endpoint selection (e.g. "
            "'routing_tier:local-coder'), resolved from the routing authority."
        ),
    )
    event_timestamp: datetime = Field(
        description="Score event timestamp; stored as last_scored (tz-aware UTC)."
    )

    @field_validator("event_timestamp")
    @classmethod
    def validate_tz_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("event_timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @property
    def is_controlled(self) -> bool:
        """True when the row is a controlled intervention eligible for a claim."""
        return self.attribution_class is EnumRowAttributionClass.CONTROLLED_INTERVENTION


__all__ = [
    "EnumRowAttributionClass",
    "ModelScoredRuntimeRow",
]
