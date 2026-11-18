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
    """Judgement rungs, including the legacy Claude label used for typed refusals."""

    LOCAL = "local"
    JEV = "jev"
    GLM = "glm"
    SONNET = "sonnet"


#: History has a delegated summary alongside the deterministic ledger references.
MODEL_SELECTED_TIERS: frozenset[EnumLaneBriefTier] = frozenset(
    {EnumLaneBriefTier.EXEMPLAR, EnumLaneBriefTier.LANE_HISTORY}
)


class EnumLaneBriefRefusal(StrEnum):
    INVALID_REQUEST = "invalid_request"
    CLAUDE_ASSEMBLER = "claude_assembler"


__all__ = [
    "MODEL_SELECTED_TIERS",
    "EnumLaneBriefRefusal",
    "EnumLaneBriefRung",
    "EnumLaneBriefTier",
    "EnumLaneBriefTierMode",
]
