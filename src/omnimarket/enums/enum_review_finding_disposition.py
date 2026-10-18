# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How a fix worker answered one hostile-reviewer finding (OMN-17427)."""

from enum import StrEnum, unique


@unique
class EnumReviewFindingDisposition(StrEnum):
    """The author's answer to one review finding.

    ``wrong`` and ``not_applicable`` are the noise answers: the reviewer raised
    something the author did not have to act on.
    """

    FIXED = "fixed"
    WRONG = "wrong"
    INTENTIONAL = "intentional"
    NOT_APPLICABLE = "not_applicable"


NOISE_DISPOSITIONS: frozenset[EnumReviewFindingDisposition] = frozenset(
    {
        EnumReviewFindingDisposition.WRONG,
        EnumReviewFindingDisposition.NOT_APPLICABLE,
    }
)

__all__ = ["NOISE_DISPOSITIONS", "EnumReviewFindingDisposition"]
