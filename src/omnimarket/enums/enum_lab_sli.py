# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Enums of the lab service-level indicators: severity, status, comparator, action."""

from __future__ import annotations

from enum import StrEnum


class EnumLabSliSeverity(StrEnum):
    """How bad a breach of one indicator is. Each indicator carries exactly one."""

    SEV1 = "SEV1"  # the lab cannot land or place anything
    SEV2 = "SEV2"  # throughput degraded
    SEV3 = "SEV3"  # drift
    SEV4 = "SEV4"  # informational


class EnumLabSliStatus(StrEnum):
    """The verdict of one indicator for one interval."""

    OK = "ok"
    BREACH = "breach"
    # No reading arrived: the source is dark. Never read as OK.
    UNKNOWN = "unknown"
    # The indicator's precondition does not hold (e.g. no work queued).
    NOT_APPLICABLE = "not_applicable"


class EnumLabSliComparator(StrEnum):
    """Which side of the target is a breach."""

    MAX = "max"  # breach when value > target
    MIN = "min"  # breach when value < target


class EnumLabSliActionKind(StrEnum):
    """What the alert node asks for."""

    PUSH_OPERATOR = "push_operator"
    FIX_LANE = "fix_lane"
    BATCHED_FIX_LANE = "batched_fix_lane"


__all__ = [
    "EnumLabSliActionKind",
    "EnumLabSliComparator",
    "EnumLabSliSeverity",
    "EnumLabSliStatus",
]
