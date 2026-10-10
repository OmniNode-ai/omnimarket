# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reasons lab-fill work is skipped (OMN-20662)."""

from enum import StrEnum


class EnumLabFillSkipReason(StrEnum):
    """First matching selection gate."""

    OWNED = "owned"
    HANDED_OFF = "handed-off"
    UNCHANGED_INPUT = "unchanged-input"
    DISPATCH_LIMIT = "dispatch-limit"
    OUT_OF_SCOPE = "out-of-repository-scope"
    IMPLEMENTATION_MERGED = "implementation-merged"
