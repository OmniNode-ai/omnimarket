# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelOccAutoauthorWindowRequest — the N-window counter input (OMN-14393).

The read-only observation trail the window aggregator counts over. Pure input,
zero I/O. Two mutually exclusive input shapes:

  * ``records`` (preferred, OMN-14954): the tuple-keyed append-only raw log.
    The aggregator dedupes to distinct exact source tuples itself and can
    verify the representative composition floor (>=3 merged-path, >=1
    runtime/deploy-gated inside the trailing clean streak).
  * ``observations`` (legacy): bare payloads with no tuple identity. The
    streak is still reported, but composition is unverifiable, so this shape
    can never certify ``flip_ready`` (fail-closed).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.events.occ_autoauthor import ModelOccAutoauthorObservation
from omnimarket.events.occ_observation_record import ModelOccObservationRecord


class ModelOccAutoauthorWindowRequest(BaseModel):
    """Input to the OCC auto-authoring window counter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observations: tuple[ModelOccAutoauthorObservation, ...] = Field(
        default=(),
        description=(
            "LEGACY input: bare observation payloads (any order). Streak-only — "
            "no tuple identity, so composition is unverifiable and flip_ready "
            "is withheld (OMN-14954). Mutually exclusive with `records`."
        ),
    )
    records: tuple[ModelOccObservationRecord, ...] = Field(
        default=(),
        description=(
            "PREFERRED input (OMN-14954): the tuple-keyed append-only raw log. "
            "Deduplicated to distinct exact source tuples by the aggregator; "
            "carries verification_path for the composition floor. Mutually "
            "exclusive with `observations`."
        ),
    )
    required_streak: int = Field(
        default=10,
        ge=1,
        description="N: consecutive clean machine-minted passes required to declare flip_ready (design §4, default 10, operator-adjustable).",
    )
    min_merged_path: int = Field(
        default=3,
        ge=0,
        description=(
            "Composition floor: minimum merged-path records inside the trailing "
            "clean streak (rolling-plan A7 default 3)."
        ),
    )
    min_runtime_gated: int = Field(
        default=1,
        ge=0,
        description=(
            "Composition floor: minimum runtime/deploy-gated records inside the "
            "trailing clean streak (rolling-plan A7 default 1)."
        ),
    )

    @model_validator(mode="after")
    def _forbid_ambiguous_double_input(self) -> ModelOccAutoauthorWindowRequest:
        if self.observations and self.records:
            raise ValueError(
                "records and observations are mutually exclusive — supply the "
                "tuple-keyed records (preferred) or the legacy bare "
                "observations, never both"
            )
        return self


__all__ = ["ModelOccAutoauthorWindowRequest"]
