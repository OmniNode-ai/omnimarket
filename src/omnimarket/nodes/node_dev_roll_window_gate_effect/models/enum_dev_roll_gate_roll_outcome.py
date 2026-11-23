# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How the gate's last roll ended."""

from enum import StrEnum


class EnumDevRollGateRollOutcome(StrEnum):
    """The end of a released roll: the agent's completion, or the gate giving up on one."""

    FINISHED_HEALTHY = "finished_healthy"
    FINISHED_UNHEALTHY = "finished_unhealthy"
    ABANDONED = "abandoned"
