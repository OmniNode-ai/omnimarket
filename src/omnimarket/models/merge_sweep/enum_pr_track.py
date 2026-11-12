# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Classification track for a PR (OMN-23854)."""

from __future__ import annotations

from enum import StrEnum


class EnumPRTrack(StrEnum):
    """Classification track for a PR."""

    A_UPDATE = "A-update"
    A_MERGE = "A"
    A_RESOLVE = "A-resolve"
    B_POLISH = "B"
    SKIP = "skip"


__all__ = ["EnumPRTrack"]
