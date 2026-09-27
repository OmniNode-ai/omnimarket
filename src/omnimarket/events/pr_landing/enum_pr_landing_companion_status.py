# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""State of the change-control companion bound to a product PR."""

from __future__ import annotations

from enum import StrEnum


class EnumPrLandingCompanionStatus(StrEnum):
    """The companion statuses the workflow row carries."""

    NONE = "none"
    PENDING = "pending"
    OPEN = "open"
    CONFLICTING = "conflicting"
    MERGED = "merged"
    CLOSED = "closed"
    DECLINED = "declined"


__all__: list[str] = ["EnumPrLandingCompanionStatus"]
