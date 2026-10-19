# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Anchor status and loss reasons for review finding identity (OMN-21190)."""

from __future__ import annotations

from enum import StrEnum


class EnumAnchorStatus(StrEnum):
    """Where a finding's anchor stands after identity resolution."""

    NEW = "NEW"
    UNCHANGED = "UNCHANGED"
    MOVED = "MOVED"
    ANCHOR_LOST = "ANCHOR_LOST"


class EnumAnchorLostReason(StrEnum):
    """Why an anchor could not be re-found. ANCHOR_LOST is unlabelable for any anchor-dependent outcome."""

    HUNK_NOT_FOUND = "HUNK_NOT_FOUND"
    FILE_UNAVAILABLE = "FILE_UNAVAILABLE"
    NO_ANCHOR_LINES = "NO_ANCHOR_LINES"
