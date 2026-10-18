# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Priority of a task-kind rule; the highest-priority match names the task kind."""

from __future__ import annotations

from enum import StrEnum


class EnumContextTaskKindPriority(StrEnum):
    """CRITICAL > HIGH > MEDIUM > LOW (port item AP-S4); ties break by rule id."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def rank(self) -> int:
        """0 for CRITICAL up to 3 for LOW; a lower rank wins."""
        return _RANK[self]


_RANK: dict[EnumContextTaskKindPriority, int] = {
    EnumContextTaskKindPriority.CRITICAL: 0,
    EnumContextTaskKindPriority.HIGH: 1,
    EnumContextTaskKindPriority.MEDIUM: 2,
    EnumContextTaskKindPriority.LOW: 3,
}


__all__ = ["EnumContextTaskKindPriority"]
