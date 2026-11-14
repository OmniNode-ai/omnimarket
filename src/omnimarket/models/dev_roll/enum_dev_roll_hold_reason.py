# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why a pending batch of tip events is not rolled yet."""

from enum import StrEnum


class EnumDevRollHoldReason(StrEnum):
    """One reason per hold, in the order the handler checks them."""

    NO_PENDING_EVENTS = "no_pending_events"
    ROLL_IN_PROGRESS = "roll_in_progress"
    WINDOW_OPEN = "window_open"
    MIN_START_INTERVAL = "min_start_interval"
    PREVIOUS_ROLL_NOT_HEALTHY = "previous_roll_not_healthy"
