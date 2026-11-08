# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dispatch state of one fill entry."""

from __future__ import annotations

from enum import StrEnum


class EnumFloorFillEntryState(StrEnum):
    """`none` is an entry with a ticket and no dispatch state."""

    NONE = "none"
    RESERVED = "RESERVED"
    SENT = "SENT"
