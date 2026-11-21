# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Finding kinds and outcomes of the ticket hand-edit alarm."""

from __future__ import annotations

from enum import StrEnum


class EnumTicketHandEditFindingKind(StrEnum):
    """The two typed errors this node raises."""

    TICKET_HAND_EDIT = "ticket_hand_edit"
    DETECTOR_UNREADABLE = "detector_unreadable"


class EnumTicketHandEditOutcome(StrEnum):
    """What one handled message did."""

    EVALUATED = "evaluated"
    THROTTLED = "throttled"
    REFUSED = "refused"
    UNREADABLE = "unreadable"
    RECORDED = "recorded"
    IGNORED = "ignored"
