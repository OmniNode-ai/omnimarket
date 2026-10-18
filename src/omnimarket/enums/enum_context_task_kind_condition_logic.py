# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How a task-kind rule combines its conditions."""

from __future__ import annotations

from enum import StrEnum


class EnumContextTaskKindConditionLogic(StrEnum):
    """Condition logic of a task-kind rule; any other value fails validation."""

    ALL = "all"
    ANY = "any"


__all__ = ["EnumContextTaskKindConditionLogic"]
