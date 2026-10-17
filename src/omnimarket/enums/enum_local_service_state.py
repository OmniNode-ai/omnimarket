# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""What an observer saw of one local service at one moment."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumLocalServiceState(StrEnum):
    """The observed state, before any freshness rule: an observer reports only what it saw."""

    RUNNING = "running"
    EXITED_OK = "exited_ok"
    DOWN = "down"
    UNHEALTHY = "unhealthy"


__all__ = ["EnumLocalServiceState"]
