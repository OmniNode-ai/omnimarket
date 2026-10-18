# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why the external dead-man raises (OMN-20808, plan 4.8)."""

from __future__ import annotations

from enum import StrEnum


class EnumExternalDeadmanReason(StrEnum):
    """One condition that opens or extends the external dead-man's issue."""

    #: Both legs' mirror variables are older than the staleness bound, or unreadable.
    BOTH_MIRRORS_STALE = "both_mirrors_stale"
    #: A fresh mirror reports an episode undelivered past its delivery-due time plus the bound.
    EPISODE_UNDELIVERED = "episode_undelivered"
    #: A fresh mirror reports an episode unrecorded past its delivery-due time plus the bound.
    EPISODE_UNRECORDED = "episode_unrecorded"
    #: The day's canary episode is past its receipt deadline and lacks a required event.
    CANARY_INCOMPLETE = "canary_incomplete"
