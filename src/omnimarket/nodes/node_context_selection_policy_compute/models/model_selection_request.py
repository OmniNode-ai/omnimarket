# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Request model for context-selection policy compute (OMN-12843 / M3).

The node is pure: callers resolve effectiveness scores from the M2 capsule
store (``node_projection_capsule_store``) and pass them in. The node does no
I/O. Each candidate carries the M2 stable identity (``capsule_hash``) plus its
resolved effectiveness score, whether the profile declares it required, and any
experiment arm binding.
Profile-declared rules may instead admit candidates without a measured score.
"""

from __future__ import annotations

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_context_selection_policy_compute.models.model_selection_rule_ref import (
    ModelSelectionRuleRef,
)


class ModelContextCandidate(BaseModel):
    """A single candidate context factor offered to the selection policy.

    ``effectiveness_score`` is the M2-resolved measured score for this capsule
    (``None`` for the explicit fallback or profile-declared scope-rule case).
    ``is_required`` reflects the profile's required-factor declaration.
    ``forced_experiment_cohort`` binds this candidate to an experiment arm; when
    set, the candidate is an arm-fixed selection (must match the active cohort).
    ``scope_rule_ref`` identifies the profile-declared scope, status or task-kind
    rule admitting this candidate without a score or experiment arm binding.
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
            "M2-resolved measured effectiveness in [0, 1]. None for "
            "FALLBACK_NO_SCORE or POLICY_SCOPE_RULE candidates."
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
    scope_rule_ref: ModelSelectionRuleRef | None = Field(
        default=None,
        description=(
            "The profile-declared rule admitting this candidate by scope, status "
            "or task kind, without a score or experiment arm binding."
        ),
    )

    @model_validator(mode="after")
    def _enforce_scope_rule_authority(self) -> ModelContextCandidate:
        """Keep scope-rule authority separate from scores and experiment arms."""
        if self.scope_rule_ref is not None:
            if self.effectiveness_score is not None:
                raise ValueError(
                    "scope_rule_ref must not be combined with effectiveness_score."
                )
            if self.forced_experiment_cohort is not None:
                raise ValueError(
                    "scope_rule_ref must not be combined with forced_experiment_cohort."
                )
        return self


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
    model_ranking: tuple[str, ...] | None = Field(
        default=None,
        description=(
            "Model ranking of optional candidates as candidate source ids, never text."
        ),
    )
    max_optional: int | None = Field(
        default=None,
        ge=0,
        description="How many optional candidates to keep; None keeps all eligible.",
    )
    request_id: str = Field(
        default="",
        description="Correlation id echoed onto the result.",
    )


__all__ = [
    "ModelContextCandidate",
    "ModelContextSelectionRequest",
]
