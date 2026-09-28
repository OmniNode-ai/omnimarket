# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The verdict of one paired comparison of two rungs."""

from __future__ import annotations

from enum import StrEnum


class EnumComparisonVerdict(StrEnum):
    """NO_DIFFERENCE, DIFFERENCE, or REFUSED.

    NO_DIFFERENCE: the exact test did not find a difference at the stated
    confidence. DIFFERENCE: it did, in the direction ``difference`` reports.
    REFUSED: the comparison is not a valid result at all (fewer prompts than the
    power analysis requires, a method not sized by it, or no pairs).
    """

    NO_DIFFERENCE = "no_difference"
    DIFFERENCE = "difference"
    REFUSED = "refused"
