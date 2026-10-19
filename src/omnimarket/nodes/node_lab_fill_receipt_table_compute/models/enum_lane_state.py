# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The state a lane's own receipt puts it in (OMN-17427)."""

from __future__ import annotations

from enum import StrEnum


class EnumLaneState(StrEnum):
    """Where a dispatched lane stands, read from its receipt and never from a count."""

    PLACED = "placed"
    REFUSED = "refused"
    PREPARING = "preparing"
    NEVER_STARTED = "never-started"
    NO_HOST = "no-host"
    HOST_LIMITED = "host-limited"
