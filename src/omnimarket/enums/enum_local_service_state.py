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
    # The observer's own statement that a service is no longer in the lane's roster. It is an instruction
    # to the projection writer to forget the service's row and clear its alerts, never a stored state.
    RETIRED = "retired"


__all__ = ["EnumLocalServiceState"]
