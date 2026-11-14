# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two outcomes of one batching decision."""

from enum import StrEnum


class EnumDevRollAction(StrEnum):
    """Roll now at the newest ref, or hold with a typed reason."""

    ROLL_NOW = "roll_now"
    HOLD = "hold"
