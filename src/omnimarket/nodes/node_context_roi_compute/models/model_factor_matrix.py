# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""N-arm factor matrix for the context-ROI experiment (OMN-12797 P2-3).

The matrix is the canonical authority for which arms exist, their factor
composition, and their required/optional declarations. It is constructed once
via `build_canonical_factor_matrix()` and passed to the scorer unchanged.

Factor precedence within the pack builder is:
    GOLDEN_CHAIN > EXEMPLAR > LOCAL_FAILURES > ARCHITECTURE_PATTERNS > CLAUDE_MD

This ordering is the existing deterministic precedence in
`node_context_pack_builder_compute` (handler:45-68) and is enforced there.
The matrix declares factor membership per arm; the pack builder enforces order.

The `full_guidance_negative_control` arm:
  - is_negative_control=True (never the preferred arm)
  - budget behavior: the existing TOKEN_BUDGET_EXCEEDED hard-reject fires if
    the full CLAUDE.md content exceeds the 16k token budget; the arm records
    failure_stage='budget_fail', scored separately from generation failures.
    Never silently truncated.
"""

from __future__ import annotations

from omnibase_core.enums.enum_context_factor import EnumContextFactor

from omnimarket.nodes.node_context_roi_compute.models.model_factor_arm import (
    EnumArmLabel,
    ModelFactorArm,
)


def build_canonical_factor_matrix() -> tuple[ModelFactorArm, ...]:
    """Construct the canonical N-arm factor matrix.

    Returns a deterministic, frozen tuple of ModelFactorArm instances in the
    canonical arm order. The order is significant: it determines the default
    iteration order in result tables and reports.

    Arm order (matches the P2-3 spec table, top to bottom):
      1. off
      2. golden_only
      3. golden_exemplar
      4. golden_exemplar_failures
      5. structured_context
      6. structured_plus_guidance_chunks
      7. full_guidance_negative_control
    """
    return (
        ModelFactorArm(
            label=EnumArmLabel.OFF,
            factors=(),
            required_factors=(),
            optional_factors=(),
            is_negative_control=False,
            notes=(
                "Baseline arm: no context injected. "
                "All other arms are compared against this one."
            ),
        ),
        ModelFactorArm(
            label=EnumArmLabel.GOLDEN_ONLY,
            factors=(EnumContextFactor.GOLDEN_CHAIN,),
            required_factors=(EnumContextFactor.GOLDEN_CHAIN,),
            optional_factors=(),
            is_negative_control=False,
            notes=(
                "Golden chain only. "
                "Missing GOLDEN_CHAIN fails the arm row (not a warning)."
            ),
        ),
        ModelFactorArm(
            label=EnumArmLabel.GOLDEN_EXEMPLAR,
            factors=(
                EnumContextFactor.GOLDEN_CHAIN,
                EnumContextFactor.EXEMPLAR,
            ),
            required_factors=(EnumContextFactor.GOLDEN_CHAIN,),
            optional_factors=(EnumContextFactor.EXEMPLAR,),
            is_negative_control=False,
            notes=(
                "Golden chain + exemplar. "
                "GOLDEN_CHAIN required; EXEMPLAR optional (warns if absent)."
            ),
        ),
        ModelFactorArm(
            label=EnumArmLabel.GOLDEN_EXEMPLAR_FAILURES,
            factors=(
                EnumContextFactor.GOLDEN_CHAIN,
                EnumContextFactor.EXEMPLAR,
                EnumContextFactor.LOCAL_FAILURES,
            ),
            required_factors=(EnumContextFactor.GOLDEN_CHAIN,),
            optional_factors=(
                EnumContextFactor.EXEMPLAR,
                EnumContextFactor.LOCAL_FAILURES,
            ),
            is_negative_control=False,
            notes=(
                "Golden chain + exemplar + local failures. "
                "GOLDEN_CHAIN required; EXEMPLAR and LOCAL_FAILURES optional."
            ),
        ),
        ModelFactorArm(
            label=EnumArmLabel.STRUCTURED_CONTEXT,
            factors=(
                EnumContextFactor.GOLDEN_CHAIN,
                EnumContextFactor.EXEMPLAR,
                EnumContextFactor.LOCAL_FAILURES,
                EnumContextFactor.ARCHITECTURE_PATTERNS,
            ),
            required_factors=(EnumContextFactor.GOLDEN_CHAIN,),
            optional_factors=(
                EnumContextFactor.EXEMPLAR,
                EnumContextFactor.LOCAL_FAILURES,
                EnumContextFactor.ARCHITECTURE_PATTERNS,
            ),
            is_negative_control=False,
            notes=(
                "Structured context: adds ARCHITECTURE_PATTERNS over the previous arm. "
                "GOLDEN_CHAIN required; remaining factors optional."
            ),
        ),
        ModelFactorArm(
            label=EnumArmLabel.STRUCTURED_PLUS_GUIDANCE_CHUNKS,
            factors=(
                EnumContextFactor.GOLDEN_CHAIN,
                EnumContextFactor.EXEMPLAR,
                EnumContextFactor.LOCAL_FAILURES,
                EnumContextFactor.ARCHITECTURE_PATTERNS,
                EnumContextFactor.CLAUDE_MD,
            ),
            required_factors=(EnumContextFactor.GOLDEN_CHAIN,),
            optional_factors=(
                EnumContextFactor.EXEMPLAR,
                EnumContextFactor.LOCAL_FAILURES,
                EnumContextFactor.ARCHITECTURE_PATTERNS,
                EnumContextFactor.CLAUDE_MD,
            ),
            is_negative_control=False,
            notes=(
                "Structured context + selected CLAUDE.md guidance chunks. "
                "GOLDEN_CHAIN required; guidance chunks are pre-selected sections, "
                "not the full file. CLAUDE_MD optional (warns if absent)."
            ),
        ),
        ModelFactorArm(
            label=EnumArmLabel.FULL_GUIDANCE_NEGATIVE_CONTROL,
            factors=(EnumContextFactor.CLAUDE_MD,),
            required_factors=(),
            optional_factors=(EnumContextFactor.CLAUDE_MD,),
            is_negative_control=True,
            notes=(
                "Full CLAUDE.md only — explicitly labeled waste/negative-control. "
                "Never the preferred arm. "
                "If the full file exceeds the 16k token budget the pack builder's "
                "TOKEN_BUDGET_EXCEEDED hard-reject fires; "
                "failure_stage='budget_fail' is recorded and scored separately "
                "from generation failures. Never silently truncated."
            ),
        ),
    )


def arm_by_label(
    matrix: tuple[ModelFactorArm, ...],
    label: EnumArmLabel,
) -> ModelFactorArm:
    """Return the arm with the given label, or raise KeyError."""
    for arm in matrix:
        if arm.label == label:
            return arm
    raise KeyError(f"no arm with label {label!r} in matrix")


__all__ = [
    "arm_by_label",
    "build_canonical_factor_matrix",
]
