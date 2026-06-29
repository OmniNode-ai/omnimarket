# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Result model for context-selection policy compute (OMN-12843 / M3).

Output is an ordered, reason-annotated selection. Each entry carries the
Context Authority Rule 5-tuple so injection is auditable per run:

    {factor, source, selection_reason, effectiveness_score, experiment_cohort}

The ``effectiveness_score is None`` XOR ``selection_reason == FALLBACK_NO_SCORE``
invariant is enforced by a model validator: no silent defaults.
"""

from __future__ import annotations

from enum import StrEnum

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_selection_reason import EnumSelectionReason
from pydantic import BaseModel, ConfigDict, Field, model_validator


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
            "selection_reason == FALLBACK_NO_SCORE."
        ),
    )
    experiment_cohort: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Cohort id when selection_reason == EXPERIMENT_ASSIGNMENT; None otherwise."
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
        has_score = self.effectiveness_score is not None
        # effectiveness_score is None XOR selection_reason == FALLBACK_NO_SCORE.
        if is_fallback and has_score:
            raise ValueError(
                "FALLBACK_NO_SCORE selection must not carry an effectiveness_score."
            )
        if not is_fallback and not has_score:
            raise ValueError(
                f"selection_reason {self.selection_reason.value!r} requires a "
                "non-null effectiveness_score (no silent defaults)."
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
