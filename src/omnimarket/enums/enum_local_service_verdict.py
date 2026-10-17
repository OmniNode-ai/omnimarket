# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The verdict the dashboard shows for one local service."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumLocalServiceVerdict(StrEnum):
    """The observed state with the freshness deadline applied: a report older than it is STALE."""

    RUNNING = "running"
    DOWN = "down"
    UNHEALTHY = "unhealthy"
    STALE = "stale"


__all__ = ["EnumLocalServiceVerdict"]
