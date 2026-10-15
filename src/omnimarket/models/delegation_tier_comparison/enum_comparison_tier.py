# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Claude model tier a task class is mapped to, cheapest first."""

from __future__ import annotations

from enum import StrEnum


class EnumComparisonTier(StrEnum):
    """Closed set, ordered by price: haiku, then sonnet, then opus."""

    HAIKU = "haiku"
    SONNET = "sonnet"
    OPUS = "opus"

    @property
    def rank(self) -> int:
        return _RANK[self]


_RANK = {
    EnumComparisonTier.HAIKU: 0,
    EnumComparisonTier.SONNET: 1,
    EnumComparisonTier.OPUS: 2,
}
