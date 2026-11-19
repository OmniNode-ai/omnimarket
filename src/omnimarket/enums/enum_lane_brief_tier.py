# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tiers of the lane-brief context profile, how each is read, and the rungs a model-selected tier walks."""

from __future__ import annotations

from enum import StrEnum


class EnumLaneBriefTier(StrEnum):
    """The seven tiers a code lane's context pack carries, in pack order."""

    EXEMPLAR = "exemplar"
    GRAPH_ANSWERS = "graph_answers"
    REVIEW_FINDINGS = "review_findings"
    FILES_TO_TOUCH = "files_to_touch"
    TESTS_AND_CI = "tests_and_ci"
    LANE_HISTORY = "lane_history"
    RISKS = "risks"


class EnumLaneBriefTierMode(StrEnum):
    """Deterministic tiers are read by code and never call a model; a model-selected tier walks the rungs."""

    DETERMINISTIC = "deterministic"
    MODEL_SELECTED = "model_selected"


class EnumLaneBriefRung(StrEnum):
    """The delegation chain of a model-selected tier, in order: lab GPU hosts, then GLM, then Sonnet."""

    LOCAL = "local"
    GLM = "glm"
    SONNET = "sonnet"


#: The one model-selected tier; every other tier is read by code.
MODEL_SELECTED_TIERS: frozenset[EnumLaneBriefTier] = frozenset(
    {EnumLaneBriefTier.EXEMPLAR}
)

__all__ = [
    "MODEL_SELECTED_TIERS",
    "EnumLaneBriefRung",
    "EnumLaneBriefTier",
    "EnumLaneBriefTierMode",
]
