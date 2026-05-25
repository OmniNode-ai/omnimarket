# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Freshness state enum shared by knowledge health probe and compute nodes."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumKnowledgeFreshnessState(StrEnum):
    """Health freshness classification for a knowledge backend."""

    FRESH = "fresh"
    STALE = "stale"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"
