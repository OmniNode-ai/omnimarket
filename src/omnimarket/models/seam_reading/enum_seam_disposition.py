# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What a side did with one id that crossed a seam."""

from __future__ import annotations

from enum import StrEnum


class EnumSeamDisposition(StrEnum):
    HANDED_OVER = "handed-over"
    ACCEPTED = "accepted"
    STALE = "stale"
    BLOCKED = "blocked"
    REFUSED = "refused"
    UNUSED = "unused"

    @property
    def sender_side(self) -> bool:
        return self in (EnumSeamDisposition.HANDED_OVER, EnumSeamDisposition.UNUSED)
