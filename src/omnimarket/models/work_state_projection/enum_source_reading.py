# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How one source stood in a projection run."""

from __future__ import annotations

from enum import StrEnum


class EnumSourceReading(StrEnum):
    # Read inside its budget in this run.
    FRESH = "fresh"
    # Not read in this run; the last snapshot (within its declared maximum age) is served, with its age.
    STALE = "stale"
    # Not read and no usable snapshot: the source is named in `unread`.
    UNREAD = "unread"


__all__ = ["EnumSourceReading"]
