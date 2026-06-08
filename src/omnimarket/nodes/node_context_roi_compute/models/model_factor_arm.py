# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""N-arm factor matrix models for the context-ROI experiment (OMN-12797 P2-3).

Each arm declares:
  - label: stable string identifier (e.g. 'off', 'golden_only')
  - factors: ordered tuple of EnumContextFactor values (empty = baseline)
  - required_factors: factors that MUST be present in the context pack;
      a missing required factor fails the arm row, not a warning
  - optional_factors: factors that MAY be absent; absence emits a warning
  - is_negative_control: when True, this arm is a named waste/negative-control;
      it is never the preferred arm and budget failures are scored separately
  - notes: human-readable rationale for the arm configuration

Factor order within an arm is deterministic (tuple ordering preserved).
The full-guidance negative-control arm includes a budget_token_limit field;
if the assembled pack exceeds the 16k budget, the existing TOKEN_BUDGET_EXCEEDED
hard-reject fires and the arm records failure_stage=budget_fail — scored
separately from generation failures, never silently truncated.
"""

from __future__ import annotations

from enum import StrEnum

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumArmLabel(StrEnum):
    """Canonical arm labels for the N-arm factor matrix.

    Labels are stable identifiers used in result rows, reports, and fixtures.
    Do not rename without updating all consumers.
    """

    OFF = "off"
    GOLDEN_ONLY = "golden_only"
    GOLDEN_EXEMPLAR = "golden_exemplar"
    GOLDEN_EXEMPLAR_FAILURES = "golden_exemplar_failures"
    STRUCTURED_CONTEXT = "structured_context"
    STRUCTURED_PLUS_GUIDANCE_CHUNKS = "structured_plus_guidance_chunks"
    FULL_GUIDANCE_NEGATIVE_CONTROL = "full_guidance_negative_control"


class ModelFactorArm(BaseModel):
    """One arm of the N-arm factor matrix.

    required_factors: factors that MUST appear in the assembled context pack.
        A missing required factor causes the handler to fail the row immediately
        (failure_stage='missing_required_factor'), not emit a warning.
    optional_factors: factors whose absence is acceptable but MUST be warned
        (never silently green). The union of required_factors + optional_factors
        is the declared factor universe for this arm; any factor present in
        `factors` must appear in one of these two sets.
    is_negative_control: marks the arm as a waste / negative-control baseline.
        It MUST NOT be ranked as the preferred arm in any result summary.
        Budget failures on this arm are recorded with failure_stage='budget_fail'
        and scored in a separate category from generation failures.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: EnumArmLabel = Field(description="Stable identifier for this arm")
    factors: tuple[EnumContextFactor, ...] = Field(
        default_factory=tuple,
        description=(
            "Ordered context factors for this arm. "
            "Empty tuple = off/baseline (no context injected)."
        ),
    )
    required_factors: tuple[EnumContextFactor, ...] = Field(
        default_factory=tuple,
        description=(
            "Factors that must be present in the resolved artifacts. "
            "Missing required factor -> fail the row, not a warning."
        ),
    )
    optional_factors: tuple[EnumContextFactor, ...] = Field(
        default_factory=tuple,
        description=(
            "Factors whose absence is acceptable but must be warned. "
            "Never silently green when absent."
        ),
    )
    is_negative_control: bool = Field(
        default=False,
        description=(
            "True = this arm is a waste/negative-control baseline. "
            "MUST NOT be ranked as the preferred arm. "
            "Budget failures scored separately."
        ),
    )
    notes: str = Field(
        default="",
        description="Human-readable rationale for this arm configuration.",
    )

    @model_validator(mode="after")
    def _validate_factor_policy(self) -> ModelFactorArm:
        required_set = set(self.required_factors)
        optional_set = set(self.optional_factors)
        factors_set = set(self.factors)

        # required and optional must be disjoint
        overlap = required_set & optional_set
        if overlap:
            raise ValueError(
                f"required_factors and optional_factors overlap: "
                f"{', '.join(f.value for f in sorted(overlap, key=lambda x: x.value))}"
            )

        # every factor in `factors` must be declared in required or optional
        undeclared = factors_set - required_set - optional_set
        if undeclared:
            raise ValueError(
                f"factors contains undeclared entries (not in required_factors "
                f"or optional_factors): "
                f"{', '.join(f.value for f in sorted(undeclared, key=lambda x: x.value))}"
            )

        # full_guidance_negative_control must be marked is_negative_control
        if (
            self.label == EnumArmLabel.FULL_GUIDANCE_NEGATIVE_CONTROL
            and not self.is_negative_control
        ):
            raise ValueError(
                "full_guidance_negative_control arm must have is_negative_control=True"
            )

        return self


__all__ = [
    "EnumArmLabel",
    "ModelFactorArm",
]
