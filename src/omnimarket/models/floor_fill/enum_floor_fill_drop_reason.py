# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why an observation was dropped or a reserve refused: the row and its seq stay as they were."""

from __future__ import annotations

from enum import StrEnum


class EnumFloorFillDropReason(StrEnum):
    # A response that does not belong to the entry as it stands.
    NO_ENTRY = "no-entry"
    STALE_ATTEMPT = "stale-attempt"
    WRONG_STATE = "wrong-state"
    # The typed refusals of a reserve.
    NO_TICKET = "no-ticket"
    HELD = "held"
    WINDOW = "window"
    UNCLEARED = "uncleared"
    CAPACITY = "capacity"
    NO_SLOT = "no-slot"
    LANDING_ONLY = "landing-only"
    STOP_SIGNAL = "stop-signal"
