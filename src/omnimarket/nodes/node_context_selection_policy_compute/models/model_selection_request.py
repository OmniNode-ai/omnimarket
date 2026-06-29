# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Request model for context-selection policy compute (OMN-12843 / M3).

The node is pure: callers resolve effectiveness scores from the M2 capsule
store (``node_projection_capsule_store``) and pass them in. The node does no
I/O. Each candidate carries the M2 stable identity (``capsule_hash``) plus its
resolved effectiveness score, whether the profile declares it required, and any
experiment arm binding.
"""

from __future__ import annotations

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from pydantic import BaseModel, ConfigDict, Field


class ModelContextCandidate(BaseModel):
    """A single candidate context factor offered to the selection policy.

    ``effectiveness_score`` is the M2-resolved measured score for this capsule
    (``None`` when the M2 store has no score yet — the explicit fallback case).
    ``is_required`` reflects the profile's required-factor declaration.
    ``forced_experiment_cohort`` binds this candidate to an experiment arm; when
    set, the candidate is an arm-fixed selection (must match the active cohort).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    factor: EnumContextFactor = Field(
        description="Context factor category for this candidate."
    )
    source: str = Field(
        min_length=1,
        description=(
            "Stable capsule/source id from the M2 store (the capsule_hash for "
            "scored capsules, or another stable provenance id)."
        ),
    )
    effectiveness_score: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "M2-resolved measured effectiveness in [0, 1]. None ONLY when the "
            "M2 store has no score for this capsule yet (FALLBACK_NO_SCORE)."
        ),
    )
    is_required: bool = Field(
        description=(
            "True when the profile declares this factor required "
            "(factor_precedence / required_factors)."
        ),
    )
    forced_experiment_cohort: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Experiment cohort id this candidate is arm-fixed to, if any. When "
            "set, the candidate is selected as an EXPERIMENT_ASSIGNMENT and must "
            "match the request's active_experiment_cohort."
        ),
    )


class ModelContextSelectionRequest(BaseModel):
    """Input to the context-selection policy compute node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidates: tuple[ModelContextCandidate, ...] = Field(
        min_length=1,
        description="Candidate context factors to rank and select.",
    )
    active_experiment_cohort: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "The active experiment cohort id for this run, if a context "
            "experiment is in effect. Required for any arm-fixed candidate."
        ),
    )
    request_id: str = Field(
        default="",
        description="Correlation id echoed onto the result.",
    )


__all__ = [
    "ModelContextCandidate",
    "ModelContextSelectionRequest",
]
