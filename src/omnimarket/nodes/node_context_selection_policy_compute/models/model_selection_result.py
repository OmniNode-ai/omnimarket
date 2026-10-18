# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Result model for context-selection policy compute (OMN-12843 / M3).

Output is an ordered, reason-annotated selection. Each entry carries the
Context Authority Rule 5-tuple so injection is auditable per run:

    {factor, source, selection_reason, effectiveness_score, experiment_cohort}

Only ``FALLBACK_NO_SCORE`` and ``POLICY_SCOPE_RULE`` may carry a null
effectiveness score, and both require it. ``POLICY_SCOPE_RULE`` additionally
requires a profile rule reference; no other reason may carry one. These
invariants are enforced by a model validator: no silent defaults.
"""

from __future__ import annotations

from enum import StrEnum

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_selection_reason import EnumSelectionReason
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_context_selection_policy_compute.models.model_selection_rule_ref import (
    ModelSelectionRuleRef,
)


class EnumSelectionStatus(StrEnum):
    """Terminal status of a selection request."""

    OK = "ok"
    ERROR = "error"


class ModelFactorSelection(BaseModel):
    """One selected context factor carrying the Authority 5-tuple."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    factor: EnumContextFactor = Field(description="Selected context factor.")
    source: str = Field(
        min_length=1,
        description="Stable capsule/source id from the M2 store.",
    )
    selection_reason: EnumSelectionReason = Field(
        description="Typed authority reason this factor was selected.",
    )
    effectiveness_score: float | None = Field(
        ge=0.0,
        le=1.0,
        description=(
            "M2-resolved measured effectiveness. None ONLY when "
            "selection_reason is FALLBACK_NO_SCORE or POLICY_SCOPE_RULE."
        ),
    )
    experiment_cohort: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Cohort id when selection_reason == EXPERIMENT_ASSIGNMENT; None otherwise."
        ),
    )
    rule_ref: ModelSelectionRuleRef | None = Field(
        default=None,
        description=(
            "Profile rule admitting a POLICY_SCOPE_RULE selection; None otherwise."
        ),
    )
    rank: int = Field(
        ge=1,
        description="1-indexed rank in the ordered selection.",
    )

    @model_validator(mode="after")
    def _enforce_authority_invariants(self) -> ModelFactorSelection:
        """Enforce the Context Authority Rule invariants (no silent defaults)."""
        is_fallback = self.selection_reason == EnumSelectionReason.FALLBACK_NO_SCORE
        is_scope_rule = self.selection_reason == EnumSelectionReason.POLICY_SCOPE_RULE
        has_score = self.effectiveness_score is not None
        # Only fallback and scope-rule selections may carry null scores.
        if is_fallback and has_score:
            raise ValueError(
                "FALLBACK_NO_SCORE selection must not carry an effectiveness_score."
            )
        if is_scope_rule and has_score:
            raise ValueError(
                "POLICY_SCOPE_RULE selection must not carry an effectiveness_score."
            )
        if not is_fallback and not is_scope_rule and not has_score:
            raise ValueError(
                f"selection_reason {self.selection_reason.value!r} requires a "
                "non-null effectiveness_score (no silent defaults)."
            )
        if is_scope_rule and self.rule_ref is None:
            raise ValueError("POLICY_SCOPE_RULE selection requires a rule_ref.")
        if not is_scope_rule and self.rule_ref is not None:
            raise ValueError(
                f"rule_ref is only valid for POLICY_SCOPE_RULE, not "
                f"{self.selection_reason.value!r}."
            )
        # EXPERIMENT_ASSIGNMENT requires a cohort; no other reason may carry one.
        is_experiment = (
            self.selection_reason == EnumSelectionReason.EXPERIMENT_ASSIGNMENT
        )
        has_cohort = self.experiment_cohort is not None
        if is_experiment and not has_cohort:
            raise ValueError(
                "EXPERIMENT_ASSIGNMENT selection requires an experiment_cohort."
            )
        if not is_experiment and has_cohort:
            raise ValueError(
                f"experiment_cohort is only valid for EXPERIMENT_ASSIGNMENT, not "
                f"{self.selection_reason.value!r}."
            )
        return self


class ModelContextSelectionResult(BaseModel):
    """Output of the context-selection policy compute node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumSelectionStatus = Field(description="ok or error.")
    selections: tuple[ModelFactorSelection, ...] = Field(
        default=(),
        description=(
            "Ordered, reason-annotated selection. Empty when status == error."
        ),
    )
    selection_rejected: int = Field(
        default=0,
        ge=0,
        description=(
            "Count of model rankings ignored for ids outside the eligible optional "
            "set or duplicate ids."
        ),
    )
    selection_rejected_ids: tuple[str, ...] = Field(
        default=(),
        description="Offending model ranking ids, sorted and unique.",
    )
    request_id: str = Field(
        default="",
        description="Echo of the request correlation id.",
    )
    error: str | None = Field(
        default=None,
        description="Error detail when status == error.",
    )

    @model_validator(mode="after")
    def _enforce_status_consistency(self) -> ModelContextSelectionResult:
        """status==error iff an error string is present and there are no selections."""
        if self.status == EnumSelectionStatus.ERROR:
            if self.error is None:
                raise ValueError("error status requires an error message.")
            if self.selections:
                raise ValueError("error status must not carry selections.")
        else:
            if self.error is not None:
                raise ValueError("ok status must not carry an error message.")
        return self


__all__ = [
    "EnumSelectionStatus",
    "ModelContextSelectionResult",
    "ModelFactorSelection",
]
