# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Factor sets and the model-role authority rule shared by context profiles."""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Set as AbstractSet

from omnibase_core.enums.enum_context_factor import EnumContextFactor

#: Factors of plan section 4.2 rows 1 to 5: bytes depend only on the pinned commit.
STABLE_TIER_FACTORS: frozenset[EnumContextFactor] = frozenset(
    {
        EnumContextFactor.LANE_RULES,
        EnumContextFactor.INVARIANTS,
        EnumContextFactor.ADRS,
        EnumContextFactor.CANONICAL_SHAPES,
        EnumContextFactor.ARCHITECTURE_PATTERNS,
        EnumContextFactor.EXEMPLAR,
    }
)
#: Factors a model writes (row 10); they render last and are trimmed first.
MODEL_WRITTEN_FACTORS: frozenset[EnumContextFactor] = frozenset(
    {EnumContextFactor.TAIL_SUMMARY}
)
#: Model roles may enrich a package; they cannot provide its required authority.
MODEL_ROLE_FACTOR_FIELDS: dict[EnumContextFactor, str] = {
    EnumContextFactor.TRANSCRIPT_EXTRACTS: "extraction_task_class",
    EnumContextFactor.TAIL_SUMMARY: "summary_task_class",
}


def reject_required_model_role_factors(
    required: AbstractSet[EnumContextFactor],
    task_classes: Mapping[str, str | None],
) -> None:
    """Raise ValueError when a declared model role would produce a required factor."""
    for factor, role_field in MODEL_ROLE_FACTOR_FIELDS.items():
        if factor in required and task_classes.get(role_field) is not None:
            raise ValueError(
                f"{role_field} cannot produce required factor {factor.value}; "
                "model-role factors must be optional"
            )


__all__ = [
    "MODEL_ROLE_FACTOR_FIELDS",
    "MODEL_WRITTEN_FACTORS",
    "STABLE_TIER_FACTORS",
    "reject_required_model_role_factors",
]
