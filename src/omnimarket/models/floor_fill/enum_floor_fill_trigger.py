# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The trigger each accepted observation takes: one per row of the contract's state_machine."""

from __future__ import annotations

from enum import StrEnum


class EnumFloorFillTrigger(StrEnum):
    TICKET_STORED = "ticket_stored"
    RESERVE_ADMITTED = "reserve_admitted"
    CONFIRMED = "confirmed"
    RESERVED_WAITING = "reserved_waiting"
    RESERVED_TIMED_OUT = "reserved_timed_out"
    START_FAILED = "start_failed"
    SENT_LIVE = "sent_live"
    SENT_ENDED = "sent_ended"
    SENT_EXPIRED = "sent_expired"
