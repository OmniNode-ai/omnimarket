# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How one heartbeat's evaluation ended."""

from __future__ import annotations

from enum import StrEnum


class EnumWorkProgressOutcome(StrEnum):
    """Exactly one outcome per heartbeat."""

    EVALUATED = "evaluated"
    THROTTLED = "throttled"
    REFUSED = "refused"
    UNREADABLE = "unreadable"
