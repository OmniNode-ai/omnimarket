# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Freshness states derived from independent supply and demand (OMN-19513)."""

from enum import StrEnum


class EnumFreshnessState(StrEnum):
    """UNKNOWN is an observation failure; only STALE asks for Slack."""

    IDLE = "IDLE"
    FRESH = "FRESH"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"
