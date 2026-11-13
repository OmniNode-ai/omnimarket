# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The outcome of one dev roll terminal."""

from enum import StrEnum


class EnumDevRollOutcome(StrEnum):
    """What the terminal says about the roll."""

    RECOVERED = "recovered"
    UNRECOVERED = "unrecovered"
    UNKNOWN = "unknown"
