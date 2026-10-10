# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Whether one gate fact was read (OMN-20866)."""

from __future__ import annotations

from enum import StrEnum


class EnumPrLandingFactState(StrEnum):
    """KNOWN: the projection answered. UNKNOWN: it could not be read; fail closed."""

    KNOWN = "known"
    UNKNOWN = "unknown"


__all__: list[str] = ["EnumPrLandingFactState"]
