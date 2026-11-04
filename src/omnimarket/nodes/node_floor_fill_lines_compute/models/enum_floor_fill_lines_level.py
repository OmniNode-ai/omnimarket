# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The level of one finding, the first cell of the tick's finding tuple."""

from __future__ import annotations

from enum import StrEnum


class EnumFloorFillLinesLevel(StrEnum):
    """The tick prints the level upper-cased: MISSING, NOTE, UNKNOWN."""

    MISSING = "missing"
    NOTE = "note"
    UNKNOWN = "unknown"
