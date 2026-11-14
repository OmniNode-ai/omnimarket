# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What one handled input did."""

from __future__ import annotations

from enum import StrEnum


class EnumTicketHandEditOutcome(StrEnum):
    WRITE_RECORDED = "write_recorded"
    STALE_WRITE_IGNORED = "stale_write_ignored"
    SWEPT = "swept"
    UNREADABLE = "unreadable"
    THROTTLED = "throttled"
    REFUSED = "refused"
