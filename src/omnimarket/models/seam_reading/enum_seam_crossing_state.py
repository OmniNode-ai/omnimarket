# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""State of one crossing read at a seam."""

from __future__ import annotations

from enum import StrEnum


class EnumSeamCrossingState(StrEnum):
    MATCHED = "matched"
    MISMATCHED = "mismatched"
    UNREADABLE = "unreadable"
