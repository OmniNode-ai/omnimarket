# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reasons lab-fill work is skipped (OMN-20662).

Shared since OMN-20864: the idle-slot pr-land fallback rule, which lab-fill selection and the
merge-throughput tick both run, decides with the same reasons.
"""

from enum import StrEnum


class EnumLabFillSkipReason(StrEnum):
    """First matching selection gate."""

    OWNED = "owned"
    HANDED_OFF = "handed-off"
    UNCHANGED_INPUT = "unchanged-input"
    DISPATCH_LIMIT = "dispatch-limit"
    OUT_OF_SCOPE = "out-of-repository-scope"
    IMPLEMENTATION_MERGED = "implementation-merged"
    # OMN-20864: an in-force HOLD row names the PR or its repository.
    SKIP_HELD = "held"
    # OMN-20864: the PR's red, or its last landing lane's block, is a cause no lane can fix on it.
    SKIP_BLOCKED_UNFIXABLE = "blocked-unfixable"
