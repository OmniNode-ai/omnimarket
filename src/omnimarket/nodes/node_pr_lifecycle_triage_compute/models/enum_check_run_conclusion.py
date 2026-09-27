# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EnumCheckRunConclusion: the check-runs API ``conclusion`` of one copy."""

from __future__ import annotations

from enum import StrEnum


class EnumCheckRunConclusion(StrEnum):
    """A completed check-run's ``conclusion``."""

    SUCCESS = "success"
    FAILURE = "failure"
    NEUTRAL = "neutral"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    TIMED_OUT = "timed_out"
    ACTION_REQUIRED = "action_required"
    STALE = "stale"
    STARTUP_FAILURE = "startup_failure"


# Conclusions that pass a required context.
CHECK_RUN_PASSING_CONCLUSIONS: frozenset[EnumCheckRunConclusion] = frozenset(
    {
        EnumCheckRunConclusion.SUCCESS,
        EnumCheckRunConclusion.NEUTRAL,
        EnumCheckRunConclusion.SKIPPED,
    }
)


__all__: list[str] = ["CHECK_RUN_PASSING_CONCLUSIONS", "EnumCheckRunConclusion"]
