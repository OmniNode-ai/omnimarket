# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dispatch state of a fill entry, as the line compute reads it."""

from __future__ import annotations

from enum import StrEnum


class EnumFloorFillLinesEntryState(StrEnum):
    """`none` is an entry with a ticket and no dispatch state."""

    NONE = "none"
    RESERVED = "RESERVED"
    SENT = "SENT"
