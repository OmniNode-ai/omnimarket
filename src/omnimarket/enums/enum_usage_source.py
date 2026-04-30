# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Usage source enum for LLM cost attribution."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumUsageSource(StrEnum):
    """Source quality for token and cost usage attribution."""

    MEASURED = "measured"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> EnumUsageSource | None:
        if not isinstance(value, str):
            return None

        legacy_aliases = {
            "API": cls.MEASURED,
            "api": cls.MEASURED,
            "ESTIMATED": cls.ESTIMATED,
            "MISSING": cls.UNKNOWN,
            "missing": cls.UNKNOWN,
        }
        return legacy_aliases.get(value)
