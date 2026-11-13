# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The verdict of one dev roll against the declared start bound."""

from enum import StrEnum


class EnumDevRollVerdict(StrEnum):
    """What the counted runtime starts say about one finished dev roll."""

    PASSED = "passed"
    TOO_MANY_STARTS = "too_many_starts"
    NO_START = "no_start"
    START_COUNT_UNKNOWN = "start_count_unknown"
