# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why the landing workflow hands a PR to an agent."""

from __future__ import annotations

from enum import StrEnum


class EnumPrLandingAgentReason(StrEnum):
    """The four agent-needed reasons of the transition table."""

    COMPANION_DECLINED = "companion_declined"
    COMPANION_ERROR = "companion_error"
    REAL_RED = "real_red"
    STALLED = "stalled"


__all__: list[str] = ["EnumPrLandingAgentReason"]
