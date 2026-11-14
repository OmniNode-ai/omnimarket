# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which scorer produced a scored item (OMN-24170)."""

from __future__ import annotations

from enum import StrEnum


class EnumEvalScorer(StrEnum):
    """The scorer behind one scored try."""

    CONTRACT_CHECKS = "contract_checks"
    ANSWER_FIELDS = "answer_fields"
    REVIEWER_MODEL = "reviewer_model"


__all__ = ["EnumEvalScorer"]
