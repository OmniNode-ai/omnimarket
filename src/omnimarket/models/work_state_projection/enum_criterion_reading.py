# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How a start criterion reads."""

from __future__ import annotations

from enum import StrEnum


class EnumCriterionReading(StrEnum):
    """How a start criterion reads."""

    TRUE = "true"
    FALSE = "false"
    UNREAD = "unread"
