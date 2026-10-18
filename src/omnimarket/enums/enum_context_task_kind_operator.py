# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Operators a task-kind rule condition may apply to a caller-scope field."""

from __future__ import annotations

from enum import StrEnum


class EnumContextTaskKindOperator(StrEnum):
    """Typed condition operators (plan section 4.4; port item AP-S4).

    ``equals`` is an exact string comparison, never a substring test. ``glob`` is a
    path glob and applies only to the path fields of the caller scope.
    """

    EQUALS = "equals"
    GLOB = "glob"


__all__ = ["EnumContextTaskKindOperator"]
